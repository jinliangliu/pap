# tests/integration/test_chat_attachments.py
"""对话附件全链路：接收 → 落盘成句柄 → 消息记录 → 原文件回取。

这里刻意走 HTTP 而非直接调 service：
附件的体积限制、错误分类、URL 生成分别落在 schema / service / route 三层，
只测其中一层会漏掉「接口收得下、服务存不住」这类接缝问题。
"""

from __future__ import annotations

import base64
import json
import re
import uuid

import pytest

from app.agents.planner import reset_planner, set_planner_for_tests
from app.agents.schemas import ActionPlan
from app.core.config import settings
from app.services import attachment_store
from app.tools import ocr_tools

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
_PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def _data_url(raw: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


class _RecordingPlanner:
    """捕获送给模型的 prompt，用来确认附件说明真的进了上下文。

    create_action_plan 传的是 (role, text) 元组而非消息对象，
    两种形态都收，免得以后换实现时测试静默变成「什么都没捕获到」。
    """

    def __init__(self, response: str = "好的。") -> None:
        self.response = response
        self.prompts: list[str] = []

    async def ainvoke(self, messages):  # noqa: ANN001
        for message in messages:
            if isinstance(message, tuple):
                self.prompts.append(str(message[1]))
                continue
            content = getattr(message, "content", "")
            self.prompts.append(
                content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            )
        return ActionPlan(action="chat", response=self.response)


@pytest.fixture
def attachment_dir(tmp_path, monkeypatch):
    """附件根目录必须隔离：否则测试会往仓库的 data/uploads 里写文件。"""
    monkeypatch.setattr(settings, "ocr_attachment_dir", str(tmp_path))
    yield tmp_path
    reset_planner()


@pytest.fixture
def recording_planner():
    planner = _RecordingPlanner()
    set_planner_for_tests(planner)
    yield planner
    reset_planner()


async def _conversation(client, thread_id: str) -> dict:
    listing = await client.get("/api/v1/conversations?limit=100")
    items = listing.json()["items"]
    matched = [c for c in items if c["thread_id"] == thread_id]
    assert matched, f"会话 {thread_id} 未出现在列表里"
    detail = await client.get(f"/api/v1/conversations/{matched[0]['id']}")
    return detail.json()


async def test_image_attachment_is_stored_and_prompted(client, attachment_dir, recording_planner):
    thread_id = f"att-image-{uuid.uuid4().hex[:8]}"

    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": thread_id,
            "message": "这张表里写了什么？",
            "attachments": [{"data_url": _data_url(_PNG, "image/png"), "name": "现场.png"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"

    # 模型必须看到句柄，且被明确告知用 ocr_document 去读——否则它只能凭空描述
    prompt = "\n".join(recording_planner.prompts)
    assert "[附件]" in prompt
    assert "ocr_document" in prompt
    assert "img_" in prompt

    detail = await _conversation(client, thread_id)
    user_message = [m for m in detail["messages"] if m["role"] == "user"][0]
    assert len(user_message["attachments"]) == 1

    attachment = user_message["attachments"][0]
    assert attachment["kind"] == "image"
    assert attachment["mime"] == "image/png"
    assert attachment["name"] == "现场.png"
    # 文件名只用于回显，不能变成磁盘路径
    assert "/" not in attachment["attachment_id"]

    # 落盘的是原始字节，不是 base64
    files = list(attachment_dir.rglob("img_*.png"))
    assert len(files) == 1
    assert files[0].read_bytes() == _PNG


async def test_pdf_attachment_is_stored_as_pdf(client, attachment_dir, recording_planner):
    thread_id = f"att-pdf-{uuid.uuid4().hex[:8]}"

    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": thread_id,
            "message": "这份报表第三页的编号是多少？",
            "attachments": [
                {"data_url": _data_url(_PDF, "application/pdf"), "name": "日报.pdf"}
            ],
        },
    )

    assert response.status_code == 200

    prompt = "\n".join(recording_planner.prompts)
    assert "PDF" in prompt
    assert "doc_" in prompt

    detail = await _conversation(client, thread_id)
    user_message = [m for m in detail["messages"] if m["role"] == "user"][0]
    attachment = user_message["attachments"][0]
    assert attachment["kind"] == "pdf"
    assert attachment["mime"] == "application/pdf"
    assert list(attachment_dir.rglob("doc_*.pdf"))


async def test_multiple_attachments_keep_order(client, attachment_dir, recording_planner):
    thread_id = f"att-multi-{uuid.uuid4().hex[:8]}"

    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": thread_id,
            "message": "对比这两份材料。",
            "attachments": [
                {"data_url": _data_url(_PNG, "image/png"), "name": "一.png"},
                {"data_url": _data_url(_PDF, "application/pdf"), "name": "二.pdf"},
                {"data_url": _data_url(_PNG, "image/png"), "name": "三.png"},
            ],
        },
    )

    assert response.status_code == 200

    detail = await _conversation(client, thread_id)
    user_message = [m for m in detail["messages"] if m["role"] == "user"][0]
    assert [a["name"] for a in user_message["attachments"]] == ["一.png", "二.pdf", "三.png"]


async def test_attachment_download_returns_original_bytes(client, attachment_dir, recording_planner):
    thread_id = f"att-get-{uuid.uuid4().hex[:8]}"

    await client.post(
        "/api/v1/chat",
        json={
            "thread_id": thread_id,
            "message": "看一下附件。",
            "attachments": [{"data_url": _data_url(_PDF, "application/pdf")}],
        },
    )

    detail = await _conversation(client, thread_id)
    user_message = [m for m in detail["messages"] if m["role"] == "user"][0]
    url = user_message["attachments"][0]["url"]
    assert url

    fetched = await client.get(url)

    assert fetched.status_code == 200
    assert fetched.headers["content-type"] == "application/pdf"
    # 必须是 inline：attachment 会让浏览器直接下载，气泡里的预览就空白了
    assert fetched.headers["content-disposition"].startswith("inline")
    assert fetched.content == _PDF


async def test_attachment_url_rejects_unknown_handle(client, attachment_dir, recording_planner):
    thread_id = f"att-404-{uuid.uuid4().hex[:8]}"

    await client.post(
        "/api/v1/chat",
        json={
            "thread_id": thread_id,
            "message": "看一下附件。",
            "attachments": [{"data_url": _data_url(_PNG, "image/png")}],
        },
    )

    detail = await _conversation(client, thread_id)
    conversation_id = detail["conversation"]["id"]

    # 句柄不是凭据：格式合法但不存在的 id 必须 404，不能用默认值凑一个路径
    response = await client.get(
        f"/api/v1/conversations/{conversation_id}/attachments/img_ffffffffffff"
    )
    assert response.status_code == 404

    # 目录穿越同理
    response = await client.get(
        f"/api/v1/conversations/{conversation_id}/attachments/..%2F..%2Fetc%2Fpasswd"
    )
    assert response.status_code == 404


async def test_invalid_attachment_rejects_without_creating_conversation(client, attachment_dir):
    thread_id = f"att-bad-{uuid.uuid4().hex[:8]}"
    before = (await client.get("/api/v1/conversations?limit=100")).json()["total"]

    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": thread_id,
            "message": "看附件。",
            "attachments": [
                # 压缩包既不是图片/PDF，也不是文本：没有读取工具能处理它。
                # （纯文本曾经是这里的反例，现在 Markdown / txt 是支持的附件。）
                {"data_url": "data:application/zip;base64,UEsDBAoAAAAAAA=="}
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rejected"
    assert body["error_category"] == "invalid_attachment"
    assert "附件" in body["answer"]

    # 校验放在建会话之前：拒绝的请求不该留下一条空会话
    after = (await client.get("/api/v1/conversations?limit=100")).json()["total"]
    assert after == before


async def test_too_many_attachments_rejected(client, attachment_dir, monkeypatch):
    monkeypatch.setattr(settings, "chat_max_attachments", 2)

    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": f"att-many-{uuid.uuid4().hex[:8]}",
            "message": "看附件。",
            "attachments": [
                {"data_url": _data_url(_PNG, "image/png")} for _ in range(3)
            ],
        },
    )

    assert response.json()["status"] == "rejected"
    assert response.json()["error_category"] == "invalid_attachment"


async def test_deleting_conversation_purges_attachment_files(client, attachment_dir, recording_planner):
    thread_id = f"att-del-{uuid.uuid4().hex[:8]}"

    await client.post(
        "/api/v1/chat",
        json={
            "thread_id": thread_id,
            "message": "看附件。",
            "attachments": [{"data_url": _data_url(_PNG, "image/png")}],
        },
    )

    detail = await _conversation(client, thread_id)
    conversation_id = detail["conversation"]["id"]
    assert list(attachment_dir.rglob("img_*.png"))

    response = await client.delete(f"/api/v1/conversations/{conversation_id}")
    assert response.status_code == 200

    # 消息记录已经没人能读到，文件继续留着只会一直占盘
    remaining = [
        p for p in attachment_dir.rglob("*") if p.is_file()
    ]
    assert remaining == []


async def test_legacy_images_field_still_works(client, attachment_dir, recording_planner):
    """老客户端仍传 images，不能升级后直接 422。"""
    thread_id = f"att-legacy-{uuid.uuid4().hex[:8]}"

    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": thread_id,
            "message": "看图片。",
            "images": [{"data_url": _data_url(_PNG, "image/png")}],
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"

    detail = await _conversation(client, thread_id)
    user_message = [m for m in detail["messages"] if m["role"] == "user"][0]
    assert len(user_message["attachments"]) == 1


def test_handle_prefix_matches_kind():
    png, png_mime = attachment_store.decode_data_url(
        _data_url(_PNG, "image/png"), max_image_bytes=1024 * 1024
    )
    assert attachment_store.kind_of(png_mime) == "image"

    pdf, pdf_mime = attachment_store.decode_data_url(
        _data_url(_PDF, "application/pdf"), max_image_bytes=1024 * 1024
    )
    assert attachment_store.kind_of(pdf_mime) == "pdf"
    assert attachment_store.suffix_for(pdf_mime) == ".pdf"


class _TwoTurnPlanner:
    """按 payload 内容决策，模拟真实模型的取值方式。

    有图片句柄就去识别，已经拿到 [本轮工具结果] 就作答，否则直接回答。
    句柄从 prompt 里抽取而不是硬编码——否则测不出「它拿到的是新句柄还是旧句柄」。
    """

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def ainvoke(self, messages):  # noqa: ANN001
        payload: dict | None = None
        for message in messages:
            if isinstance(message, tuple):
                content = str(message[1])
            else:
                content = getattr(message, "content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
            if '"user_message"' in content:
                payload = json.loads(content)

        assert payload is not None
        self.payloads.append(payload)
        user_message = str(payload.get("user_message") or "")

        if "[本轮工具结果]" in user_message:
            return ActionPlan(action="chat", response="已识别附件内容。")

        # 只看「本条消息新增」这一段：更早的材料也列在清单里，但这一轮新给的是
        # PDF。从整段里搜会先撞上上一轮那张图片的句柄——那正是这个用例要防的误读。
        added = user_message.split("本条消息新增：", 1)[-1].split(
            "本会话此前上传的材料", 1
        )[0]
        matched = re.search(r"file_id=(img_[0-9a-f]{12})", added)
        if matched:
            return ActionPlan(
                action="read_tool",
                tool_name="ocr_document",
                arguments={"file_id": matched.group(1)},
            )

        return ActionPlan(action="chat", response="已按本轮附件作答。")


async def test_second_turn_does_not_reuse_previous_ocr_result(
    client, attachment_dir, monkeypatch
):
    """同一会话先传图片、再传 PDF：第二轮不能拿上一轮的识别结果当本轮附件。

    2026-11-05 线上实录：传完「1.jpg」又问「核对一下这个站点报告」并附 PDF，
    模型回答「识别结果与文件名不符」——它看到的「已执行的 OCR」其实是上一轮
    那张调压箱照片的文本，本轮 PDF 一次都没识别，整轮只花了 3.8 秒。
    """
    planner = _TwoTurnPlanner()
    set_planner_for_tests(planner)

    async def fake_ocr(file_path, *, timeout=None):  # noqa: ANN001, ARG001
        return "调压箱/柜现场记录表 出口压力 2.3kPa 编号 00000000000-000001"

    monkeypatch.setattr(ocr_tools, "_call_ocr_mcp", fake_ocr)

    thread_id = f"att-twoturn-{uuid.uuid4().hex[:8]}"

    first = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": thread_id,
            "message": "查一下这个站点压力",
            "attachments": [{"data_url": _data_url(_PNG, "image/png"), "name": "1.jpg"}],
        },
    )
    assert first.status_code == 200
    # 第一轮确实执行了识别，并把结果回灌给了规划器
    assert first.json()["answer"] == "已识别附件内容。"

    second = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": thread_id,
            "message": "核对一下这个站点报告",
            "attachments": [
                {"data_url": _data_url(_PDF, "application/pdf"), "name": "测试报告.pdf"}
            ],
        },
    )
    assert second.status_code == 200
    # 若上一轮的结果串进来，这里会变成「已识别附件内容。」
    assert second.json()["answer"] == "已按本轮附件作答。"

    payload = planner.payloads[-1]
    assert "[本轮工具结果]" not in payload["user_message"]
    assert "调压箱" not in payload["user_message"]
    assert payload["pending_slots"] == {}
    # 本轮 PDF 的句柄必须在上下文里，模型才知道该识别哪个文件
    assert "doc_" in payload["user_message"]
    assert "测试报告.pdf" in payload["user_message"]


async def test_studio_attachments_become_conversation_materials(
    client, attachment_dir, planner
):
    """构建助手的附件是会话材料：上传后留下、跨轮可读、刷新后能点开原文。

    曾经它是「临时文件」，这一轮答完就删。改成会话材料之后，
    判据只剩一条：历史里的附件入口是不是真能取回原文件。
    """
    planner([ActionPlan(action="chat", response="截图里是 GB 27791-2020。")])
    await client.get("/api/v1/agents")  # 惰性创建内置助手

    thread_id = f"studio-att-{uuid.uuid4().hex[:8]}"
    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": thread_id,
            "message": "这张截图里的标准号是什么？",
            "agent": "studio",
            "attachments": [
                {"data_url": _data_url(_PNG, "image/png"), "name": "现场.png"}
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"

    # 这一轮返回之后文件还在：删掉就变成「用户传过、但谁也拿不回来」
    files = [p for p in attachment_dir.rglob("*") if p.is_file()]
    assert len(files) == 1
    assert files[0].name.startswith("img_")

    detail = await _conversation(client, thread_id)
    user_message = [m for m in detail["messages"] if m["role"] == "user"][0]
    assert len(user_message["attachments"]) == 1
    attachment = user_message["attachments"][0]
    assert attachment["name"] == "现场.png"

    # 挂出来的入口必须真能取回原图，否则只是一个打不开的装饰
    fetched = await client.get(attachment["url"])
    assert fetched.status_code == 200
    assert fetched.content == _PNG


async def test_studio_markdown_attachment_is_kept(
    client, attachment_dir, recording_planner
):
    """Markdown 走同一条材料规则：类型变了，生命周期不变。

    浏览器对 `.md` 常常给空 type（这里就用空 type 发），所以能否收下
    全靠服务端按后缀判定——前端 accept 只是第一道提示。
    """
    await client.get("/api/v1/agents")  # 惰性创建内置助手

    thread_id = f"studio-md-{uuid.uuid4().hex[:8]}"
    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": thread_id,
            "message": "按这份规程列出巡检项。",
            "agent": "studio",
            "attachments": [
                {"data_url": _data_url("# 巡检规程\n\n1. 每日检查压力。\n".encode(), ""), "name": "巡检规程.md"}
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "completed"

    prompt = "\n".join(recording_planner.prompts)
    # 标签必须写 Markdown 而不是笼统的「文件」：模型据此决定读不读
    assert "类型=Markdown" in prompt
    assert "txt_" in prompt
    assert "巡检规程.md" in prompt

    # 与图片/PDF 一样：文件留在会话里，下一轮还能读
    assert list(attachment_dir.rglob("txt_*"))

    detail = await _conversation(client, thread_id)
    user_message = [m for m in detail["messages"] if m["role"] == "user"][0]
    assert [a["name"] for a in user_message["attachments"]] == ["巡检规程.md"]


class _ClarifyThenAnswer:
    """第一轮反问、第二轮作答，记下每轮模型真正看到的 payload。"""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    async def ainvoke(self, messages):  # noqa: ANN001
        payload: dict | None = None
        for message in messages:
            if isinstance(message, tuple):
                content = str(message[1])
            else:
                content = getattr(message, "content", "")
                if not isinstance(content, str):
                    content = json.dumps(content, ensure_ascii=False)
            if '"user_message"' in content:
                payload = json.loads(content)

        assert payload is not None
        self.payloads.append(payload)

        if len(self.payloads) == 1:
            return ActionPlan(
                action="clarify",
                clarification="这份文档整篇入库，还是只入其中几章？",
            )
        return ActionPlan(action="chat", response="按第三章入库。")


async def test_studio_attachment_follows_clarify_into_next_turn(
    client, attachment_dir
):
    """反问之后用户回的那一句，用的还必须是同一份文件。

    2026-11-16 线上实录：国标 PDF 传上来，助手先问「入库方式要先定」，
    用户回了个「？」，得到的回答是「附件不存在或已过期，请重新上传」。
    当时是「临时附件在反问那一轮结束就删」造成的；现在附件本身是会话材料，
    这条用例仍然守着同一个承诺：反问把话头交回用户，材料得跟着交回去。
    """
    planner = _ClarifyThenAnswer()
    set_planner_for_tests(planner)
    await client.get("/api/v1/agents")  # 惰性创建内置助手

    thread_id = f"studio-carry-{uuid.uuid4().hex[:8]}"
    first = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": thread_id,
            "message": "把这份文档添加到知识库",
            "agent": "studio",
            "attachments": [
                {"data_url": _data_url(_PDF, "application/pdf"), "name": "GB3836.pdf"}
            ],
        },
    )

    assert first.status_code == 200
    assert first.json()["status"] == "needs_input"
    assert [p for p in attachment_dir.rglob("*") if p.is_file()]

    second = await client.post(
        "/api/v1/chat",
        json={"thread_id": thread_id, "message": "只入第三章", "agent": "studio"},
    )

    assert second.status_code == 200
    assert second.json()["status"] == "completed"

    # 这一句本身没带附件，模型只能从「本会话已有的材料」里拿到句柄
    prompt = str(planner.payloads[-1]["user_message"])
    assert "本会话此前上传的材料" in prompt
    assert "doc_" in prompt
    assert "GB3836.pdf" in prompt
    assert "本条消息没有新附件" in prompt

    # 两轮都还在：材料不是「接上话就作废」，第三轮照样能读
    assert [p for p in attachment_dir.rglob("*") if p.is_file()]

    detail = await _conversation(client, thread_id)
    users = [m for m in detail["messages"] if m["role"] == "user"]
    assert len(users[0]["attachments"]) == 1
    assert users[1]["attachments"] == []


async def test_studio_material_is_readable_after_a_normal_answer(
    client, attachment_dir, recording_planner
):
    """正常答完的一轮也留着材料：下一轮说「再读一遍刚才那份」不必重传。

    这是与旧规则最关键的分歧——旧实现里只有反问轮会暂留，
    正常答完就删，第三轮再问必然读不到。
    """
    await client.get("/api/v1/agents")

    thread_id = f"studio-keep-{uuid.uuid4().hex[:8]}"
    first = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": thread_id,
            "message": "这份规程讲了什么？",
            "agent": "studio",
            "attachments": [
                {"data_url": _data_url("# 巡检规程\n\n1. 每日检查压力。\n".encode(), "text/markdown"),
                 "name": "巡检规程.md"}
            ],
        },
    )
    assert first.json()["status"] == "completed"

    second = await client.post(
        "/api/v1/chat",
        json={"thread_id": thread_id, "message": "第一条是什么？", "agent": "studio"},
    )
    assert second.json()["status"] == "completed"

    prompt = "\n".join(recording_planner.prompts)
    assert "本会话此前上传的材料" in prompt
    assert "巡检规程.md" in prompt
    assert "本条消息没有新附件" in prompt


async def test_binary_renamed_as_markdown_is_rejected(client, attachment_dir):
    """改名不是通行证：`.docx` 改成 `.md` 仍然会被服务端拒掉。"""
    thread_id = f"att-fake-md-{uuid.uuid4().hex[:8]}"
    zip_header = b"PK\x03\x04\x14\x00\x00\x00\x08\x00" + b"\x00" * 32

    response = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": thread_id,
            "message": "看这份规程。",
            "attachments": [
                {"data_url": _data_url(zip_header, "application/octet-stream"), "name": "伪装.md"}
            ],
        },
    )

    body = response.json()
    assert body["status"] == "rejected"
    assert body["error_category"] == "invalid_attachment"


async def test_ocr_text_is_persisted_for_later_review(client, attachment_dir, monkeypatch):
    """识别原文要随消息落库：刷新会话后仍能展开「识别原文」核对。

    只放在当轮响应里的话，这轮过去就再也找不回来——而核对场景恰恰
    是回头逐项比对。
    """
    set_planner_for_tests(_TwoTurnPlanner())

    async def fake_ocr(file_path, *, timeout=None):  # noqa: ANN001, ARG001
        return "出口压力 2.35kPa 设定 2.30kPa"

    monkeypatch.setattr(ocr_tools, "_call_ocr_mcp", fake_ocr)

    thread_id = f"att-ocrmeta-{uuid.uuid4().hex[:8]}"
    first = await client.post(
        "/api/v1/chat",
        json={
            "thread_id": thread_id,
            "message": "看下这张记录表",
            "attachments": [{"data_url": _data_url(_PNG, "image/png"), "name": "1.jpg"}],
        },
    )
    assert first.status_code == 200
    assert first.json()["ocr_text"]

    listing = (await client.get("/api/v1/conversations")).json()["items"]
    conv = next(item for item in listing if item["thread_id"] == thread_id)
    detail = (await client.get(f"/api/v1/conversations/{conv['id']}")).json()

    assistant = [m for m in detail["messages"] if m["role"] == "assistant"][0]
    assert assistant["metadata"].get("ocr_text") == first.json()["ocr_text"]
