import asyncio, json, time, uuid
from typing import Any, Dict, List, Optional

from astrbot.api.star import register, Star, Context
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
import astrbot.api.message_components as Comp
from astrbot.api import logger

SAKURA_WS_URLS = [
    "ws://43.143.53.96:5000/onebot/v11/ws",    # 娱乐功能
    "ws://101.34.19.31:13888/onebot/v11/ws",    # pjsk
    "ws://121.41.63.60:11735/pub/onebotSocket",    # osu
]
SAKURA_ACCESS_TOKEN = ""

def _now_ts() -> int:
    return int(time.time())

@register("sakurabridge", "your_name", "Bridge to Sakura OneBot v11", "0.1.0")
class SakuraBridge(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._ws_tasks: List[asyncio.Task] = []
        self._session_map: Dict[str, str] = {}
        self._ws_peers: "set[Any]" = set()
        
    @filter.on_astrbot_loaded()
    async def _boot(self):
        logger.info("[sakurabridge] on_astrbot_loaded fired, preparing WS clients")
        import websockets
        if not SAKURA_WS_URLS:
            logger.warning("[sakurabridge] 请先在 main.py 中填写 SAKURA_WS_URLS")
            return
        for url in SAKURA_WS_URLS:
            self._ws_tasks.append(asyncio.create_task(self._run_onebot_client(url)))

    async def _run_onebot_client(self, url: str):
        import websockets
        backoff = 1
        while True:
            try:
                headers = {}
                if SAKURA_ACCESS_TOKEN:
                    headers["Authorization"] = f"Bearer {SAKURA_ACCESS_TOKEN}"
                logger.info(f"[sakurabridge] connect {url}")
                async with websockets.connect(url, extra_headers=headers) as ws:
                    self._ws_peers.add(ws)
                    await self._listen_actions(ws)
            except Exception as e:
                logger.error(f"[sakurabridge] ws {url} disconnected: {e}")
            finally:
                self._ws_peers.discard(ws) if "ws" in locals() else None
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _listen_actions(self, ws):
        async for raw in ws:
            try:
                data = json.loads(raw)
            except Exception:
                continue
            if isinstance(data, dict) and "action" in data:
                await self._handle_action(ws, data)

    async def _handle_action(self, ws, packet: Dict[str, Any]):
        action = packet.get("action")
        params = packet.get("params", {}) or {}
        echo   = packet.get("echo")
        chains = self._ob_message_to_chain(params.get("message"))
        target_key = None
        if "group_id" in params:
            gid = str(params["group_id"])
            target_key = f"group:{gid}"
        elif "user_id" in params:
            uid = str(params["user_id"])
            target_key = f"user:{uid}"

        if not target_key or target_key not in self._session_map:
            await self._send_action_resp(ws, echo, retcode=100, msg="unknown session (no recent message)")
            return

        umo = self._session_map[target_key]
        await self.context.send_message(umo, chains)
        await self._send_action_resp(ws, echo, retcode=0, msg="ok", data={"message_id": str(uuid.uuid4())})

    async def _send_action_resp(self, ws, echo: Optional[str], retcode: int, msg: str, data: Optional[dict]=None):
        resp = {"status": "ok" if retcode == 0 else "failed", "retcode": retcode, "msg": msg, "echo": echo}
        if data: resp["data"] = data
        await ws.send(json.dumps(resp, ensure_ascii=False))

    def _ob_message_to_chain(self, ob_msg) -> MessageChain:
        """
        支持两种输入：
        - str
        - OneBot v11 段列表 [{"type":"text","data":{"text":"hi"}}, {"type":"image","data":{"file":"url"}}]
        """
        chain = MessageChain()
        if not ob_msg:
            return chain
        if isinstance(ob_msg, str):
            return chain.message(ob_msg)

        if isinstance(ob_msg, list):
            for seg in ob_msg:
                t = seg.get("type"); d = seg.get("data", {}) or {}
                if t in ("text", "plain"):
                    chain.append(Comp.Plain(d.get("text","")))
                elif t == "image":
                    f = d.get("file") or d.get("url")
                    if f:
                        if str(f).startswith("http"):
                            chain.append(Comp.Image.fromURL(f))
                        else:
                            chain.append(Comp.Image.fromFileSystem(f))
        return chain

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def _forward_to_sakura(self, event: AstrMessageEvent):
        logger.debug(f"[sakurabridge] got message: {event.message_str!r}")
        if event.get_group_id():
            gid_official = event.get_group_id()
            gid_onebot   = GROUP_ID_MAP.get(gid_official, gid_official)
            self._session_map[f"group:{gid_onebot}"] = event.unified_msg_origin
        else:
            uid_official = event.get_sender_id()
            self._session_map[f"user:{uid_official}"] = event.unified_msg_origin

        ob_event = self._make_onebot_message_event(event)
        payload = json.dumps(ob_event, ensure_ascii=False)

        send_tasks = [ws.send(payload) for ws in list(self._ws_peers)]
        if send_tasks:
            await asyncio.gather(*send_tasks, return_exceptions=True)
        else:
            logger.warning("[sakurabridge] no active Sakura WS peers; did on_astrbot_loaded run?")

    def _make_onebot_message_event(self, event: AstrMessageEvent) -> Dict[str, Any]:
        is_group = bool(event.get_group_id())
        gid_official = event.get_group_id() or ""
        gid_onebot   = GROUP_ID_MAP.get(gid_official, gid_official) if is_group else ""
        ob_segments = []
        for c in event.message_obj.message:
            if isinstance(c, Comp.Plain):
                ob_segments.append({"type":"text","data":{"text":c.text}})
            elif isinstance(c, Comp.Image):
                ob_segments.append({"type":"image","data":{"file": c.file}})

        return {
            "time": _now_ts(),
            "self_id": event.message_obj.self_id or "astrbot",
            "post_type": "message",
            "message_type": "group" if is_group else "private",
            "sub_type": "normal",
            "message_id": event.message_obj.message_id,
            "user_id": event.get_sender_id(),
            "message": ob_segments if ob_segments else event.message_str,
            "raw_message": event.message_str,
            **({"group_id": gid_onebot} if is_group else {}),
            "sender": {
                "user_id": event.get_sender_id(),
                "nickname": event.get_sender_name(),
            }
        }

    @filter.command("sbping")
    async def _sbping(self, event: AstrMessageEvent):
        yield event.plain_result("sakurabridge: pong")
