from flask import Flask, request
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
from lark_oapi.api.bitable.v1 import *
from lark_oapi.api.docx.v1 import *
import json
import os
import re
import requests
from datetime import datetime, timedelta

app = Flask(__name__)

# ============================================================
# 📌 配置区域
# ============================================================

# 飞书应用凭证（从环境变量读取）
APP_ID = os.environ.get("APP_ID", "")
APP_SECRET = os.environ.get("APP_SECRET", "")

# 智谱GLM API Key
GLM_API_KEY = os.environ.get("GLM_API_KEY", "")

# 多维表格字段名
FIELD_REQUIREMENT = "需求内容"
FIELD_STATUS = "验收状态"
FIELD_OWNER = "负责人"
FIELD_ROLE = "角色"  # 如果有角色字段
STATUS_PASSED = "验收通过"

# 项目配置（根据chat_id匹配项目）
PROJECTS = {
    # chat_id: 项目配置
    "oc_xxx1": {
        "name": "项目1",
        "app_token": "你的app_token",
        "table_id": "你的table_id",
        "document_id": "你的document_id"
    },
    # 私聊测试（BusJam项目）
    "oc_c837780ca61da27e17d98d55bca4c83f": {
        "name": "BusJam",
        "app_token": "OkR6bHCAfa3JrMst4fpcHd2SnHc",
        "table_id": "tblA0oTFNEI9O2wm",
        "document_id": "P80VdXVf3oFh0oxej41cIAY3nsf"
    },
}

# 消息去重
processed_messages = set()

# ============================================================
# 飞书客户端
# ============================================================

def get_client():
    return lark.Client.builder() \
        .app_id(APP_ID) \
        .app_secret(APP_SECRET) \
        .build()

def get_tenant_access_token():
    """获取tenant_access_token"""
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={
        "app_id": APP_ID,
        "app_secret": APP_SECRET
    })
    return resp.json().get("tenant_access_token")

# ============================================================
# 读取群消息
# ============================================================

def get_chat_messages(chat_id):
    """获取群聊今日消息"""
    token = get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    
    # 获取今天0点的时间戳
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start_time = str(int(today.timestamp() * 1000))
    
    url = f"https://open.feishu.cn/open-apis/im/v1/messages"
    params = {
        "container_id_type": "chat",
        "container_id": chat_id,
        "start_time": start_time,
        "page_size": 50
    }
    
    messages = []
    try:
        resp = requests.get(url, headers=headers, params=params)
        data = resp.json()
        
        if data.get("code") == 0:
            items = data.get("data", {}).get("items", [])
            for item in items:
                msg_type = item.get("msg_type")
                sender_id = item.get("sender", {}).get("id", "")
                
                # 只处理文本消息
                if msg_type == "text":
                    content = json.loads(item.get("body", {}).get("content", "{}"))
                    text = content.get("text", "")
                    if text and not text.startswith("@"):  # 排除@消息
                        messages.append({
                            "sender_id": sender_id,
                            "text": text,
                            "time": item.get("create_time")
                        })
    except Exception as e:
        print(f"获取消息失败: {e}")
    
    return messages

# ============================================================
# 读取多维表格验收需求
# ============================================================

def get_accepted_requirements(project):
    """获取今日进行中的需求"""
    print("   正在查询多维表格...")
    
    token = get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{project['app_token']}/tables/{project['table_id']}/records/search"
    
    # 只筛选"是否今日任务"="是"的记录
    payload = {
        "filter": {
            "conjunction": "and",
            "conditions": [
                {
                    "field_name": "是否今日任务",
                    "operator": "is",
                    "value": ["是"]
                }
            ]
        },
        "page_size": 100
    }
    
    requirements = []
    
    try:
        resp = requests.post(url, headers=headers, json=payload)
        data = resp.json()
        
        print(f"   API返回: code={data.get('code')}")
        
        if data.get("code") == 0:
            items = data.get("data", {}).get("items", [])
            print(f"   获取到 {len(items)} 条今日需求")
            
            for item in items:
                fields = item.get("fields", {})
                
                # 处理需求内容字段（可能是富文本格式）
                req_name_raw = fields.get(FIELD_REQUIREMENT, "")
                if isinstance(req_name_raw, list):
                    req_name = "".join([t.get("text", "") for t in req_name_raw if isinstance(t, dict)])
                else:
                    req_name = str(req_name_raw)
                
                owner = fields.get("任务执行人", "")
                role = fields.get("部门", "其他")
                status = fields.get(FIELD_STATUS, "")
                dev_status = fields.get("开发状态", "")
                
                if isinstance(owner, list) and owner:
                    owner = owner[0].get("name", "") if isinstance(owner[0], dict) else str(owner[0])
                if isinstance(role, list) and role:
                    role = role[0] if isinstance(role[0], str) else str(role[0])
                
                requirements.append({
                    "name": req_name,
                    "owner": str(owner),
                    "role": str(role),
                    "status": str(status) if status else "",
                    "dev_status": str(dev_status) if dev_status else ""
                })
                
                print(f"   ✓ {req_name[:20]}")
        else:
            print(f"   API错误: {data}")
            
    except Exception as e:
        print(f"   获取需求异常: {e}")
        import traceback
        traceback.print_exc()
    
    return requirements

# ============================================================
# 调用GLM生成总结
# ============================================================

def call_glm_summary(messages, requirements, project_name):
    """调用智谱GLM生成日志总结"""
    
    # 构建提示词
    today = datetime.now().strftime("%Y/%m/%d")
    
    prompt = f"""你是一个产品日志助手。请根据以下信息，生成{project_name}的产品日志。

今日日期：{today}

## 今日进行中的需求：
{json.dumps(requirements, ensure_ascii=False, indent=2) if requirements else "无"}

## 今日群聊消息摘要：
{json.dumps(messages[-30:], ensure_ascii=False, indent=2) if messages else "无消息"}

请按以下格式输出日志（使用飞书文档格式）：

💡 {today}

策划：@人名1 @人名2
1. 【已完成】具体工作内容
2. 【进行中】具体工作内容

开发：@人名
1. 【已完成】具体工作内容

UI：@人名
1. 【进行中】具体工作内容

测试：@人名
1. 【已完成】具体工作内容

注意：
1. 按角色/部门分组（策划、开发、UI、测试等）
2. 根据验收状态或开发状态判断：已完成用【已完成】，未完成用【进行中】
3. 每条需求后面加上负责人 @人名
4. 只输出日志内容，不要其他解释
5. 如果没有需求，输出"💡 {today}\n今日无进行中的需求" """

    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    headers = {
        "Authorization": f"Bearer {GLM_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "glm-4-flash",  # 使用免费模型
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.7
    }
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        data = resp.json()
        
        if "choices" in data:
            return data["choices"][0]["message"]["content"]
        else:
            print(f"GLM返回错误: {data}")
            return None
    except Exception as e:
        print(f"调用GLM失败: {e}")
        return None

# ============================================================
# 写入飞书云文档（已修复）
# ============================================================

def append_to_document(document_id, content):
    """追加内容到云文档"""
    token = get_tenant_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    
    url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/blocks/{document_id}/children"
    
    # 构建文档块
    lines = content.strip().split("\n")
    blocks = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # 日期行（带💡或纯日期）作为标题
        if "💡" in line or re.match(r"^\d{4}/\d{2}/\d{2}$", line):
            blocks.append({
                "block_type": 5,  # heading3
                "heading3": {
                    "elements": [
                        {"text_run": {"content": line}}
                    ]
                }
            })
        # 有序列表项（1. 2. 3. 开头）
        elif re.match(r"^\d+\.\s", line):
            text = re.sub(r"^\d+\.\s*", "", line)
            blocks.append({
                "block_type": 13,  # ordered list（修复：16改为13）
                "ordered": {
                    "elements": [
                        {"text_run": {"content": text}}
                    ]
                }
            })
        # 子列表项（a. b. c. 开头）
        elif re.match(r"^[a-z]\.\s", line):
            text = re.sub(r"^[a-z]\.\s*", "", line)
            blocks.append({
                "block_type": 12,  # bullet list（修复：15改为12）
                "bullet": {
                    "elements": [
                        {"text_run": {"content": "  " + text}}
                    ]
                }
            })
        # 角色标题行（策划：、开发：等）
        elif re.match(r"^(策划|开发|UI|测试|产品|设计|运营)[:：]", line):
            blocks.append({
                "block_type": 2,  # text
                "text": {
                    "elements": [
                        {"text_run": {"content": line}}
                    ]
                }
            })
        # 普通文本
        else:
            blocks.append({
                "block_type": 2,  # text
                "text": {
                    "elements": [
                        {"text_run": {"content": line}}
                    ]
                }
            })
    
    try:
        resp = requests.post(url, headers=headers, json={"children": blocks})
        data = resp.json()
        
        if data.get("code") == 0:
            print("✅ 文档写入成功")
            return True
        else:
            print(f"❌ 文档写入失败: {data}")
            return False
    except Exception as e:
        print(f"❌ 写入文档异常: {e}")
        import traceback
        traceback.print_exc()
        return False

# ============================================================
# 回复消息
# ============================================================

def reply_message(message_id, text):
    """回复消息"""
    client = get_client()
    content = json.dumps({"text": text})
    
    request_body = ReplyMessageRequest.builder() \
        .message_id(message_id) \
        .request_body(ReplyMessageRequestBody.builder()
            .msg_type("text")
            .content(content)
            .build()) \
        .build()
    
    client.im.v1.message.reply(request_body)

# ============================================================
# 主处理逻辑
# ============================================================

def handle_generate_log(message):
    """处理生成日志请求"""
    chat_id = message.get("chat_id")
    message_id = message.get("message_id")
    
    print(f"\n{'='*50}")
    print(f"收到生成日志请求")
    print(f"chat_id: {chat_id}")
    
    # 查找项目配置
    project = PROJECTS.get(chat_id)
    
    if not project:
        # 如果没有配置，返回chat_id供配置使用
        reply_message(message_id, 
            f"❓ 未找到该群的配置\n\n"
            f"请将以下chat_id添加到配置中：\n"
            f"`{chat_id}`")
        return
    
    reply_message(message_id, f"⏳ 正在生成 {project['name']} 的产品日志，请稍候...")
    
    try:
        # 1. 获取群消息
        print("📨 获取群消息...")
        messages = get_chat_messages(chat_id)
        print(f"   获取到 {len(messages)} 条消息")
        
        # 2. 获取验收需求
        print("📋 获取验收需求...")
        requirements = get_accepted_requirements(project)
        print(f"   获取到 {len(requirements)} 条今日需求")
        
        # 3. 调用GLM生成总结
        print("🤖 调用GLM生成总结...")
        summary = call_glm_summary(messages, requirements, project["name"])
        
        if not summary:
            reply_message(message_id, "❌ AI总结生成失败，请重试")
            return
        
        print(f"   生成总结：\n{summary[:200]}...")
        
        # 4. 写入云文档
        print("📝 写入云文档...")
        success = append_to_document(project["document_id"], summary)
        
        if success:
            doc_url = f"https://rfc9wxlr7c.feishu.cn/docx/{project['document_id']}"
            reply_message(message_id, 
                f"✅ {project['name']} 产品日志已生成！\n\n"
                f"📊 数据来源：\n"
                f"   • 群消息：{len(messages)} 条\n"
                f"   • 今日需求：{len(requirements)} 条\n\n"
                f"📄 查看文档：{doc_url}")
        else:
            reply_message(message_id, 
                f"⚠️ 日志生成完成，但写入文档失败\n\n"
                f"生成的内容：\n{summary[:500]}...")
        
    except Exception as e:
        print(f"❌ 处理失败: {e}")
        import traceback
        traceback.print_exc()
        reply_message(message_id, f"❌ 生成失败：{str(e)}")

# ============================================================
# Webhook路由
# ============================================================

@app.route("/", methods=["GET"])
def index():
    return {
        "status": "running",
        "message": "🤖 产品日志机器人运行中",
        "projects": list(PROJECTS.keys())
    }

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    
    # 处理验证请求
    if "challenge" in data:
        return {"challenge": data["challenge"]}
    
    try:
        header = data.get("header", {})
        event = data.get("event", {})
        
        event_type = header.get("event_type")
        if event_type != "im.message.receive_v1":
            return {"code": 0}
        
        message = event.get("message", {})
        message_id = message.get("message_id", "")
        
        # 消息去重
        if message_id in processed_messages:
            return {"code": 0}
        
        # 跳过机器人消息
        sender = event.get("sender", {})
        if sender.get("sender_type") == "app":
            return {"code": 0}
        
        processed_messages.add(message_id)
        if len(processed_messages) > 1000:
            processed_messages.clear()
        
        # 解析消息内容
        content = json.loads(message.get("content", "{}"))
        text = content.get("text", "")
        
        print(f"收到消息: {text}")
        
        # 检查是否是生成日志命令
        if "生成日志" in text or "产品日志" in text or "日报" in text:
            handle_generate_log(message)
        
    except Exception as e:
        print(f"处理出错: {e}")
        import traceback
        traceback.print_exc()
    
    return {"code": 0}

# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":
    print("=" * 50)
    print("🤖 产品日志机器人 (Webhook版)")
    print("=" * 50)
    print(f"APP_ID: {APP_ID[:10]}..." if APP_ID else "APP_ID: 未配置")
    print(f"GLM_API_KEY: {'已配置' if GLM_API_KEY else '未配置'}")
    print(f"已配置 {len(PROJECTS)} 个项目")
    print("=" * 50)
    
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=False)
