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

APP_ID = os.environ.get("APP_ID", "")
APP_SECRET = os.environ.get("APP_SECRET", "")
GLM_API_KEY = os.environ.get("GLM_API_KEY", "")

# 多维表格字段名
FIELD_REQUIREMENT = "需求内容"
FIELD_STATUS = "验收状态"
FIELD_OWNER = "任务执行人"
FIELD_ROLE = "部门"
STATUS_PASSED = "验收通过"

# 项目配置
PROJECTS = {
    "oc_2575222eccd3a75f35d409eaba35ba66": {
        "name": "JigArt",
        "app_token": "Q8BWbvdpja9RzEsFXbjcXEy3nof",
        "table_id": "tbluv9XFW2P6B7sn",
        "document_id": "MTHxwrGIfiYjJHkLL4HcsBWOnPh",
        "is_wiki": True
    },
    "oc_c837780ca61da27e17d98d55bca4c83f": {
        "name": "BusJam",
        "app_token": "OkR6bHCAfa3JrMst4fpcHd2SnHc",
        "table_id": "tblA0oTFNEI9O2wm",
        "document_id": "P80VdXVf3oFh0oxej41cIAY3nsf",
        "is_wiki": False
    }
}

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
    url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
    resp = requests.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET})
    return resp.json().get("tenant_access_token")

def get_wiki_document_id(wiki_token):
    token = get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node"
    params = {"token": wiki_token}
    
    try:
        resp = requests.get(url, headers=headers, params=params)
        data = resp.json()
        if data.get("code") == 0:
            node = data.get("data", {}).get("node", {})
            return node.get("obj_token")
        return None
    except:
        return None

# ============================================================
# 读取群消息
# ============================================================

def get_chat_messages(chat_id):
    print(f"   正在获取群消息, chat_id: {chat_id}")
    
    token = get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start_time = str(int(today.timestamp()))
    
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
                msg_type = item.get("msg_type", "")
                sender = item.get("sender", {})
                sender_type = sender.get("sender_type", "user")
                
                body = item.get("body", {})
                content_str = body.get("content", "{}")
                
                text = ""
                try:
                    content = json.loads(content_str)
                    if msg_type == "text":
                        text = content.get("text", "")
                    elif msg_type == "post":
                        text = f"[富文本]{content.get('title', '')}"
                    else:
                        text = f"[{msg_type}]"
                except:
                    text = f"[{msg_type}]"
                
                messages.append({
                    "sender_type": "机器人" if sender_type == "app" else "用户",
                    "msg_type": msg_type,
                    "text": text
                })
            print(f"   获取到 {len(messages)} 条消息")
    except Exception as e:
        print(f"   获取消息异常: {e}")
    
    return messages

# ============================================================
# 读取多维表格需求
# ============================================================

def get_accepted_requirements(project):
    print("   正在查询多维表格...")
    
    token = get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{project['app_token']}/tables/{project['table_id']}/records/search"
    
    payload = {
        "filter": {
            "conjunction": "and",
            "conditions": [{
                "field_name": "是否今日任务",
                "operator": "is",
                "value": ["是"]
            }]
        },
        "page_size": 100
    }
    
    requirements = []
    
    try:
        resp = requests.post(url, headers=headers, json=payload)
        data = resp.json()
        
        if data.get("code") == 0:
            items = data.get("data", {}).get("items", [])
            print(f"   获取到 {len(items)} 条今日任务")
            
            for item in items:
                fields = item.get("fields", {})
                
                # 需求内容
                req_name_raw = fields.get(FIELD_REQUIREMENT, "")
                if isinstance(req_name_raw, list):
                    req_name = "".join([t.get("text", "") for t in req_name_raw if isinstance(t, dict)])
                else:
                    req_name = str(req_name_raw)
                
                # 验收状态
                status = fields.get(FIELD_STATUS, "")
                if isinstance(status, list) and status:
                    status = status[0] if isinstance(status[0], str) else str(status[0])
                task_status = "已完成" if status == STATUS_PASSED else "进行中"
                
                # 任务执行人 - 同时获取名字和ID
                owner_name = ""
                owner_id = ""
                owner_raw = fields.get(FIELD_OWNER, "")
                if isinstance(owner_raw, list) and owner_raw:
                    if isinstance(owner_raw[0], dict):
                        owner_name = owner_raw[0].get("name", "")
                        owner_id = owner_raw[0].get("id", "")  # 获取user_id
                    else:
                        owner_name = str(owner_raw[0])
                
                # 部门
                role = fields.get(FIELD_ROLE, "其他")
                if isinstance(role, list) and role:
                    role = role[0] if isinstance(role[0], str) else str(role[0])
                
                requirements.append({
                    "name": req_name,
                    "owner": owner_name,
                    "owner_id": owner_id,  # 新增：用户ID
                    "role": str(role),
                    "task_status": task_status
                })
                print(f"   ✓ [{task_status}] {req_name[:20]}... @{owner_name} ({role})")
        else:
            print(f"   API错误: {data}")
    except Exception as e:
        print(f"   获取需求异常: {e}")
    
    return requirements

# ============================================================
# 调用GLM生成总结
# ============================================================

def call_glm_summary(messages, requirements, project_name):
    today = datetime.now().strftime("%Y/%m/%d")
    
    in_progress = [r for r in requirements if r.get("task_status") == "进行中"]
    completed = [r for r in requirements if r.get("task_status") == "已完成"]
    
    # 构建需求文本（包含状态和部门）
    all_requirements_text = ""
    for r in completed:
        all_requirements_text += f"- 【已完成】{r['name']} @{r['owner']}（部门:{r['role']}）\n"
    for r in in_progress:
        all_requirements_text += f"- 【进行中】{r['name']} @{r['owner']}（部门:{r['role']}）\n"
    
    # 群消息
    msg_text = ""
    for m in messages[-50:]:
        if m.get("sender_type") == "机器人":
            continue
        text = m.get("text", "")
        if text and len(text) > 5:
            if "产品日志" in text or "正在生成" in text:
                continue
            msg_text += f"- {text}\n"
    
    prompt = f"""你是一个产品日志助手。请根据以下信息，生成{project_name}的产品日志。

今日日期：{today}

## 【重要】以下是今日需求列表（来自多维表格）：
{all_requirements_text if all_requirements_text else "无"}

## 今日群消息（用于提取今日要点）：
{msg_text if msg_text else "无消息"}

请严格按以下格式输出：

策划:
1. 【状态】需求名称 @负责人

UI:
1. 【状态】需求名称 @负责人

开发:
1. 【状态】需求名称 @负责人

【今日要点】
• 要点内容

输出规则：
1. 按部门分组输出需求（策划、UI、开发、测试、美术等）
2. 每条需求格式：序号. 【进行中/已完成】需求名称 @负责人
3.【已完成】和【进行中】必须原样输出多维表格中的需求，只能按部门重新分组
4. 如果某个部门没有需求，则不输出该部门
5. 【今日要点】从群消息提取重要决策、临时任务、排期变更，无则写"无"
6. 不要输出这些规则"""

    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    headers = {"Authorization": f"Bearer {GLM_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "glm-4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3
    }
    
    print("=" * 50)
    print("🤖 调用GLM API")
    print(f"   进行中: {len(in_progress)}, 已完成: {len(completed)}")
    print("=" * 50)
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        data = resp.json()
        
        if "choices" in data:
            result = data["choices"][0]["message"]["content"]
            print(f"✅ GLM调用成功!")
            return result
        else:
            print(f"❌ GLM返回错误: {data}")
            return None
    except Exception as e:
        print(f"❌ 调用GLM失败: {e}")
        return None

# ============================================================
# 写入飞书云文档
# ============================================================

def append_to_document(document_id, content, user_map=None):
    """追加内容到云文档（分割线格式，支持@人高亮）"""
    token = get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    today = datetime.now().strftime("%Y/%m/%d")
    url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/blocks/{document_id}/children"
    
    lines = content.strip().split("\n")
    blocks = []
    
    # 日期标题
    blocks.append({
        "block_type": 4,
        "heading2": {
            "elements": [{"text_run": {"content": f"📅 {today}"}}]
        }
    })
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # 跳过日期行
        if line.startswith("📅") or re.match(r"^\d{4}/\d{2}/\d{2}$", line):
            continue
        
        # 部门标题（策划: UI: 开发: 等）
        if re.match(r"^(策划|UI|开发|测试|美术|运营|其他)\s*[:：]", line):
            blocks.append({
                "block_type": 2,
                "text": {
                    "elements": [{
                        "text_run": {
                            "content": line,
                            "text_element_style": {"bold": True}
                        }
                    }]
                }
            })
        # 【今日要点】等
        elif line.startswith("【") and "】" in line:
            blocks.append({
                "block_type": 2,
                "text": {
                    "elements": [{
                        "text_run": {
                            "content": line,
                            "text_element_style": {"bold": True}
                        }
                    }]
                }
            })
        # 有序列表
        elif re.match(r"^\d+[\.\、]", line):
            text = re.sub(r"^\d+[\.\、]\s*", "", line)
            elements = parse_mention_elements(text, user_map)
            blocks.append({
                "block_type": 13,
                "ordered": {
                    "elements": elements
                }
            })
        # 无序列表
        elif line.startswith("•") or line.startswith("-"):
            text = line.lstrip("•- ").strip()
            elements = parse_mention_elements(text, user_map)
            blocks.append({
                "block_type": 12,
                "bullet": {
                    "elements": elements
                }
            })
        # 普通文本
        else:
            elements = parse_mention_elements(line, user_map)
            blocks.append({
                "block_type": 2,
                "text": {
                    "elements": elements
                }
            })
    
    # 分割线
    blocks.append({
        "block_type": 22,
        "divider": {}
    })
    
    try:
        print(f"   📝 写入 {len(blocks)} 个块...")
        resp = requests.post(url, headers=headers, json={"children": blocks})
        data = resp.json()
        
        if data.get("code") == 0:
            print("   ✅ 文档写入成功")
            return True
        else:
            print(f"   ❌ 写入失败: {data}")
            return False
    except Exception as e:
        print(f"   ❌ 写入异常: {e}")
        return False


def parse_mention_elements(text, user_map):
    """解析文本，将@人名转换为mention_user元素"""
    if not user_map or not text:
        return [{"text_run": {"content": text}}]
    
    elements = []
    # 匹配 @人名（人名可能包含中文、英文、数字）
    pattern = r'@([^\s@]+)'
    last_end = 0
    
    for match in re.finditer(pattern, text):
        # 添加@前面的文本
        if match.start() > last_end:
            elements.append({"text_run": {"content": text[last_end:match.start()]}})
        
        name = match.group(1)
        user_id = user_map.get(name)
        
        if user_id:
            # 有user_id，使用mention_user实现高亮
            elements.append({
                "mention_user": {
                    "user_id": user_id
                }
            })
        else:
            # 没有找到user_id，保持原文本
            elements.append({"text_run": {"content": match.group(0)}})
        
        last_end = match.end()
    
    # 添加最后剩余的文本
    if last_end < len(text):
        elements.append({"text_run": {"content": text[last_end:]}})
    
    # 如果没有匹配到任何内容
    if not elements:
        elements = [{"text_run": {"content": text}}]
    
    return elements

# ============================================================
# 回复消息
# ============================================================

def reply_message(message_id, text):
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
    chat_id = message.get("chat_id")
    message_id = message.get("message_id")
    
    print(f"\n{'='*50}")
    print(f"收到生成日志请求, chat_id: {chat_id}")
    
    project = PROJECTS.get(chat_id)
    if not project:
        reply_message(message_id, f"❓ 未找到该群的配置\nchat_id: `{chat_id}`")
        return
    
    try:
        # 1. 获取群消息
        print("📨 获取群消息...")
        messages = get_chat_messages(chat_id)
        
        # 2. 获取今日需求
        print("📋 获取今日需求...")
        requirements = get_accepted_requirements(project)
        
        # 3. 构建用户映射表（名字 -> user_id）
        user_map = {}
        for r in requirements:
            if r.get("owner") and r.get("owner_id"):
                user_map[r["owner"]] = r["owner_id"]
        print(f"   用户映射: {list(user_map.keys())}")
        
        # 4. 调用GLM生成总结
        print("🤖 调用GLM生成总结...")
        summary = call_glm_summary(messages, requirements, project["name"])
        
        if not summary:
            reply_message(message_id, "❌ AI总结生成失败，请重试")
            return
        
        # 5. 获取document_id
        document_id = project["document_id"]
        if project.get("is_wiki"):
            document_id = get_wiki_document_id(document_id) or document_id
        
        # 6. 写入云文档（传入user_map实现@高亮）
        print("📝 写入云文档...")
        success = append_to_document(document_id, summary, user_map)
        
        if success:
            if project.get("is_wiki"):
                doc_url = f"https://rfc9wxlr7c.feishu.cn/wiki/{project['document_id']}"
            else:
                doc_url = f"https://rfc9wxlr7c.feishu.cn/docx/{document_id}"
            
            # 回复消息不带@高亮，直接文本
            reply_message(message_id, 
                f"✅ {project['name']} 产品日志已生成！\n\n"
                f"📊 数据来源：\n"
                f"   • 群消息：{len(messages)} 条\n"
                f"   • 今日需求：{len(requirements)} 条\n\n"
                f"📝 生成内容：\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"{summary}\n"
                f"━━━━━━━━━━━━━━━━\n\n"
                f"📄 查看文档：{doc_url}")
        else:
            reply_message(message_id, f"⚠️ 日志生成完成，但写入文档失败\n\n{summary}")
        
    except Exception as e:
        print(f"❌ 处理失败: {e}")
        reply_message(message_id, f"❌ 生成失败：{str(e)}")
        
# ============================================================
# Webhook路由
# ============================================================

@app.route("/", methods=["GET"])
def index():
    return {"status": "running", "message": "🤖 产品日志机器人运行中"}

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json
    
    if "challenge" in data:
        return {"challenge": data["challenge"]}
    
    try:
        header = data.get("header", {})
        event = data.get("event", {})
        
        if header.get("event_type") != "im.message.receive_v1":
            return {"code": 0}
        
        message = event.get("message", {})
        message_id = message.get("message_id", "")
        
        if message_id in processed_messages:
            return {"code": 0}
        
        sender = event.get("sender", {})
        if sender.get("sender_type") == "app":
            return {"code": 0}
        
        processed_messages.add(message_id)
        if len(processed_messages) > 1000:
            processed_messages.clear()
        
        mentions = message.get("mentions", [])
        is_mentioned = any("产品日志" in m.get("name", "") for m in mentions)
        
        if is_mentioned:
            print(f"检测到@机器人，触发生成日志")
            handle_generate_log(message)
        
    except Exception as e:
        print(f"处理出错: {e}")
    
    return {"code": 0}

# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":
    print("=" * 50)
    print("🤖 产品日志机器人")
    print("=" * 50)
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=False)
