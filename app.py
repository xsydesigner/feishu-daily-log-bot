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
FIELD_OWNER = "任务执行人"
FIELD_ROLE = "部门"  # 如果有角色字段
STATUS_PASSED = "验收通过"

# 项目配置（根据chat_id匹配项目）
PROJECTS = {
    # JigArt项目
    "oc_2575222eccd3a75f35d409eaba35ba66": {
        "name": "JigArt",
        "app_token": "Q8BWbvdpja9RzEsFXbjcXEy3nof",
        "table_id": "tbluv9XFW2P6B7sn&view=vewENISqJi",
        "document_id": "MTHxwrGIfiYjJHkLL4HcsBWOnPh",
        "is_wiki": True
    },
    # BusJam项目
    "oc_c837780ca61da27e17d98d55bca4c83f": {
        "name": "BusJam",
        "app_token": "OkR6bHCAfa3JrMst4fpcHd2SnHc",
        "table_id": "tblA0oTFNEI9O2wm",
        "document_id": "P80VdXVf3oFh0oxej41cIAY3nsf",
        "is_wiki": False
    }
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

def get_wiki_document_id(wiki_token):
    """获取wiki文档的实际document_id"""
    token = get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    
    url = f"https://open.feishu.cn/open-apis/wiki/v2/spaces/get_node"
    params = {"token": wiki_token}
    
    try:
        resp = requests.get(url, headers=headers, params=params)
        data = resp.json()
        
        print(f"   wiki API返回: {data}")
        
        if data.get("code") == 0:
            node = data.get("data", {}).get("node", {})
            obj_token = node.get("obj_token")
            obj_type = node.get("obj_type")
            print(f"   wiki解析成功: {wiki_token} -> {obj_token} (类型:{obj_type})")
            return obj_token
        else:
            print(f"   wiki解析失败: {data}")
            return None
    except Exception as e:
        print(f"   wiki解析异常: {e}")
        return None
# ============================================================
# 读取群消息
# ============================================================

def get_chat_messages(chat_id):
    """获取群聊今日所有消息"""
    print(f"   正在获取群消息, chat_id: {chat_id}")
    
    token = get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    
    # 获取今天0点的时间戳（秒）
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
        
        print(f"   群消息API返回: code={data.get('code')}")
        
        if data.get("code") == 0:
            items = data.get("data", {}).get("items", [])
            print(f"   原始消息数: {len(items)}")
            
            for item in items:
                msg_type = item.get("msg_type", "")
                sender = item.get("sender", {})
                sender_type = sender.get("sender_type", "user")
                
                # 获取消息内容
                body = item.get("body", {})
                content_str = body.get("content", "{}")
                
                text = ""
                try:
                    content = json.loads(content_str)
                    if msg_type == "text":
                        text = content.get("text", "")
                    elif msg_type == "post":
                        # 富文本消息，提取文字
                        title = content.get("title", "")
                        text = f"[富文本]{title}"
                    elif msg_type == "image":
                        text = "[图片]"
                    elif msg_type == "file":
                        text = "[文件]"
                    elif msg_type == "interactive":
                        text = "[卡片消息]"
                    else:
                        text = f"[{msg_type}]"
                except:
                    text = f"[{msg_type}]"
                
                # 标记发送者类型
                sender_label = "机器人" if sender_type == "app" else "用户"
                
                messages.append({
                    "sender_type": sender_label,
                    "msg_type": msg_type,
                    "text": text
                })
            
            print(f"   获取到 {len(messages)} 条消息")
        else:
            print(f"   群消息API错误: {data}")
            
    except Exception as e:
        print(f"   获取消息异常: {e}")
        import traceback
        traceback.print_exc()
    
    return messages

# ============================================================
# 读取多维表格验收需求
# ============================================================

def get_accepted_requirements(project):
    """获取今日相关的需求（进行中 + 今日完成）"""
    print("   正在查询多维表格...")
    
    token = get_tenant_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{project['app_token']}/tables/{project['table_id']}/records/search"
    
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
            print(f"   获取到 {len(items)} 条今日任务")
            
            for item in items:
                fields = item.get("fields", {})
                
                # 处理需求内容
                req_name_raw = fields.get(FIELD_REQUIREMENT, "")
                if isinstance(req_name_raw, list):
                    req_name = "".join([t.get("text", "") for t in req_name_raw if isinstance(t, dict)])
                else:
                    req_name = str(req_name_raw)
                
                # 判断状态
                status = fields.get(FIELD_STATUS, "")
                if isinstance(status, list) and status:
                    status = status[0] if isinstance(status[0], str) else str(status[0])
                
                task_status = "已完成" if status == STATUS_PASSED else "进行中"
                
                # 获取任务执行人
                owner = fields.get(FIELD_OWNER, "")
                if isinstance(owner, list) and owner:
                    owner = owner[0].get("name", "") if isinstance(owner[0], dict) else str(owner[0])
                
                # 获取部门
                role = fields.get(FIELD_ROLE, "其他")
                if isinstance(role, list) and role:
                    role = role[0] if isinstance(role[0], str) else str(role[0])
                
                requirements.append({
                    "name": req_name,
                    "owner": str(owner),
                    "role": str(role),
                    "task_status": task_status
                })
                
                print(f"   ✓ [{task_status}] {req_name[:20]}... @{owner} ({role})")
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
    
    today = datetime.now().strftime("%Y/%m/%d")
    
    # 分离进行中和已完成的需求
    in_progress = [r for r in requirements if r.get("task_status") == "进行中"]
    completed = [r for r in requirements if r.get("task_status") == "已完成"]
    
    # 构建需求文本
    in_progress_text = ""
    for r in in_progress:
        in_progress_text += f"- {r['name']} @{r['owner']}（部门:{r['role']}）\n"
    
    completed_text = ""
    for r in completed:
        completed_text += f"- {r['name']} @{r['owner']}（部门:{r['role']}）\n"
    
    # 构建群消息文本 - 过滤机器人消息
    msg_text = ""
    for m in messages[-50:]:
        # 跳过机器人发送的消息
        if m.get("sender_type") == "机器人":
            continue
        text = m.get("text", "")
        if text and len(text) > 5:
            # 过滤机器人相关内容
            if "产品日志" in text or "正在生成" in text or "已生成" in text:
                continue
            msg_text += f"- {text}\n"
    
    prompt = f"""你是一个产品日志助手。请根据以下信息，生成{project_name}的产品日志。

今日日期：{today}

## 【重要】以下是今日需求列表（来自多维表格，你只能使用这些需求）：

### 已完成的需求（验收通过）：
{completed_text if completed_text else "无"}

### 进行中的需求（未验收通过）：
{in_progress_text if in_progress_text else "无"}

## 今日群消息（仅用于分析上述需求的进度，禁止从中提取新需求）：
{msg_text if msg_text else "无消息"}

请严格按以下格式输出：

策划:
1. 【状态】需求名称 @负责人

UI:
1. 【状态】需求名称 @负责人

开发:
1. 【状态】需求名称 @负责人

今日要点:
• 要点内容

【今日要点】
• 重要决策或结论
• 临时任务
• 排期变更

输出规则（不要输出这些规则）：
1. 按部门分组输出需求（策划、UI、开发、测试）
2. 每条需求格式：序号. 【进行中/已完成】需求名称 @负责人
3. 【已完成】只能列出上面"已完成的需求"中的内容
4. 【进行中】只能列出上面"进行中的需求"中的内容
5. 测试：群消息中有测试相关内容时总结输出测试进度，否则不输出"测试："
6. 【今日要点】从群消息提取重要决策、临时任务、排期变更，无则写"无"
7. 只输出日志内容，不要输出规则"""

    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    headers = {
        "Authorization": f"Bearer {GLM_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "glm-4-flash",
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3
    }
    
    print("=" * 50)
    print("🤖 调用GLM API")
    print(f"   进行中需求数: {len(in_progress)}")
    print(f"   已完成需求数: {len(completed)}")
    print("=" * 50)
    
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        data = resp.json()
        
        if "choices" in data:
            result = data["choices"][0]["message"]["content"]
            print(f"✅ GLM调用成功!")
            print(f"📥 GLM返回:\n{result}")
            return result
        else:
            print(f"❌ GLM返回错误: {data}")
            return None
    except Exception as e:
        print(f"❌ 调用GLM失败: {e}")
        import traceback
        traceback.print_exc()
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
    
    today = datetime.now().strftime("%Y/%m/%d")
    
    create_url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/blocks/{document_id}/children"
    
    # 测试更多高亮块参数格式
    test_payloads = [
        # 方案5：使用LarkMd格式的emoji
        {
            "children": [{
                "block_type": 14,
                "callout": {
                    "emoji_id": ":bulb:"
                }
            }]
        },
        # 方案6：尝试引用块代替 (block_type=17)
        {
            "children": [{
                "block_type": 17,
                "quote_container": {}
            }]
        },
        # 方案7：直接创建带内容的高亮块
        {
            "children": [{
                "block_type": 14,
                "callout": {
                    "background_color": 1,
                    "border_color": 1,
                    "emoji_id": "💡"
                }
            }],
            "index": 0
        },
        # 方案8：颜色用字符串
        {
            "children": [{
                "block_type": 14,
                "callout": {
                    "background_color": "yellow",
                    "border_color": "yellow"
                }
            }]
        }
    ]
    
    for i, payload in enumerate(test_payloads):
        print(f"\n   测试方案{i+5}:")
        print(f"   请求体: {json.dumps(payload, ensure_ascii=False)}")
        
        resp = requests.post(create_url, headers=headers, json=payload)
        data = resp.json()
        
        print(f"   响应code: {data.get('code')}")
        
        if data.get("code") == 0:
            print(f"   ✅ 方案{i+5}成功!")
            children = data.get("data", {}).get("children", [])
            if children:
                block_id = children[0].get("block_id")
                print(f"   block_id: {block_id}")
                # 成功后继续写入内容
                return write_content_to_block(document_id, block_id, content, today, headers)
    
    print("   ❌ 高亮块全部失败，使用引用块格式")
    return append_with_quote(document_id, content, today, headers)


def write_content_to_block(document_id, block_id, content, today, headers):
    """向块内写入内容"""
    url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/blocks/{block_id}/children"
    
    lines = content.strip().split("\n")
    blocks = build_content_blocks(lines, today)
    
    resp = requests.post(url, headers=headers, json={"children": blocks})
    data = resp.json()
    
    if data.get("code") == 0:
        print("   ✅ 内容写入成功")
        return True
    else:
        print(f"   ❌ 内容写入失败: {data.get('code')}")
        return False


def append_with_quote(document_id, content, today, headers):
    """备用方案：使用引用块 + 普通格式"""
    url = f"https://open.feishu.cn/open-apis/docx/v1/documents/{document_id}/blocks/{document_id}/children"
    
    lines = content.strip().split("\n")
    blocks = []
    
    # 使用二级标题作为日期标记
    blocks.append({
        "block_type": 4,
        "heading2": {
            "elements": [{"text_run": {"content": f"📅 {today}"}}]
        }
    })
    
    blocks.extend(build_content_blocks(lines, today))
    
    # 分隔线
    blocks.append({
        "block_type": 22,
        "divider": {}
    })
    
    resp = requests.post(url, headers=headers, json={"children": blocks})
    data = resp.json()
    
    if data.get("code") == 0:
        print("   ✅ 文档写入成功（备用格式）")
        return True
    else:
        print(f"   ❌ 写入失败: {data}")
        return False


def build_content_blocks(lines, today):
    """构建内容块列表"""
    blocks = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # 跳过日期行
        if line.startswith("📅") or line.startswith("💡"):
            continue
        if re.match(r"^\d{4}/\d{2}/\d{2}$", line):
            continue
        
        # 标题类（加粗）
        if (line.startswith("【") and "】" in line):
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
            blocks.append({
                "block_type": 13,
                "ordered": {
                    "elements": [{"text_run": {"content": text}}]
                }
            })
        # 无序列表
        elif line.startswith("•") or line.startswith("-"):
            text = line.lstrip("•- ").strip()
            blocks.append({
                "block_type": 12,
                "bullet": {
                    "elements": [{"text_run": {"content": text}}]
                }
            })
        # 普通文本
        else:
            blocks.append({
                "block_type": 2,
                "text": {
                    "elements": [{"text_run": {"content": line}}]
                }
            })
    
    return blocks

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
        reply_message(message_id, 
            f"❓ 未找到该群的配置\n\n"
            f"请将以下chat_id添加到配置中：\n"
            f"`{chat_id}`")
        return
    
    # 不再发送"正在生成"的消息
    
    try:
        # 1. 获取群消息
        print("📨 获取群消息...")
        messages = get_chat_messages(chat_id)
        print(f"   获取到 {len(messages)} 条消息")
        
        # 2. 获取今日需求
        print("📋 获取今日需求...")
        requirements = get_accepted_requirements(project)
        print(f"   获取到 {len(requirements)} 条今日需求")
        
        # 3. 调用GLM生成总结
        print("🤖 调用GLM生成总结...")
        summary = call_glm_summary(messages, requirements, project["name"])
        
        if not summary:
            reply_message(message_id, "❌ AI总结生成失败，请重试")
            return
        
        print(f"   生成总结：\n{summary[:200]}...")
        
        # 4. 获取实际的document_id
        document_id = project["document_id"]
        is_wiki = project.get("is_wiki", False)
        
        if is_wiki:
            print("📄 解析wiki文档...")
            real_doc_id = get_wiki_document_id(document_id)
            if not real_doc_id:
                reply_message(message_id, "❌ wiki文档解析失败，请检查权限")
                return
            document_id = real_doc_id
        
        # 5. 写入云文档
        print("📝 写入云文档...")
        success = append_to_document(document_id, summary)
        
        if success:
            # 根据类型生成文档链接
            if is_wiki:
                doc_url = f"https://rfc9wxlr7c.feishu.cn/wiki/{project['document_id']}"
            else:
                doc_url = f"https://rfc9wxlr7c.feishu.cn/docx/{document_id}"
            
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
            reply_message(message_id, 
                f"⚠️ 日志生成完成，但写入文档失败\n\n"
                f"生成的内容：\n{summary}")
        
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
        
        # mentions 在 message 层级
        mentions = message.get("mentions", [])
        print(f"mentions: {mentions}")
        
        is_mentioned = False
        for mention in mentions:
            mention_name = mention.get("name", "")
            mention_key = mention.get("key", "")
            print(f"检查mention: name={mention_name}, key={mention_key}")
            if "产品日志" in mention_name:
                is_mentioned = True
                break
        
        # @机器人就触发生成日志
        if is_mentioned:
            print(f"检测到@机器人，触发生成日志")
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
