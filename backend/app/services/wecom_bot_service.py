"""企业微信智能机器人长连接服务 — WebSocket 保活。

与「群推送 Webhook」(webhook_adapter, 单向 POST) 并存的第二条企业微信通道:
智能机器人开「API 模式 / 长连接」后, 通过 WebSocket 双向通信, 支持 @机器人
交互、流式回复、模板卡片。本服务负责连接保活(连接/鉴权/心跳/重连)与
主动推送 (aibot_send_msg), 并从消息回调里发现可用于推送的会话 chatid。

架构(对齐 depth_service):
  - daemon 线程内跑 asyncio 事件循环, 用已安装的 websockets(v16) 库
  - _running 线程存活标志 / _enabled 功能开关(持久化)
  - 指数退避重连 min(base * 2^(n-1), 60s), 与 useQuoteStream / _post_feishu 一致
  - 失败静默降级: 连接失败/凭证错误只记 WARNING, 不阻断应用启动

凭证来源: preferences.wecom_bot_id / wecom_bot_secret
连接地址: wss://openws.work.weixin.qq.com (官方固定)
协议帧(官方文档 path/101463):
  订阅 aibot_subscribe → 收 errcode=0 → 每 30s ping → 收消息/事件回调
限制: 每机器人仅 1 条长连接(新连接踢旧连接), 故配置变更需 stop→start
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from concurrent.futures import Future

logger = logging.getLogger(__name__)

# 企业微信智能机器人 WebSocket 固定连接地址
_WECOM_WS_URL = "wss://openws.work.weixin.qq.com"
# 心跳间隔(官方要求 ≤30s, 否则服务端断开)
_HEARTBEAT_INTERVAL = 30.0
# 重连退避: base * 2^(n-1), 上限 60s (与 useQuoteStream.ts 一致)
_RECONNECT_BASE_DELAY = 5.0
_RECONNECT_CAP = 60.0
_RECONNECT_MAX_ATTEMPTS = 10  # 连续失败到此次数后仍继续重连, 仅放慢节奏


class WecomBotService:
    """企业微信智能机器人 WebSocket 长连接管理器 — 单例。"""

    _instance: WecomBotService | None = None

    def __init__(self) -> None:
        WecomBotService._instance = self
        self._lock = threading.Lock()
        self._running = False          # 连接线程存活标志
        self._thread: threading.Thread | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._connected = False        # WebSocket 是否已连接并通过鉴权
        self._last_error: str = ""
        self._app_state = None         # 延迟注入, 避免循环导入
        # 主动发消息: 当前 ws + 按 req_id 等待响应的 Future (跨线程)
        self._ws = None
        self._pending: dict[str, Future] = {}

    # ================================================================
    # 生命周期
    # ================================================================

    @classmethod
    def instance(cls) -> WecomBotService | None:
        """供没有 Request/app.state 的后台任务访问同一个连接实例。"""
        return cls._instance

    def set_app_state(self, app_state) -> None:
        """注入 FastAPI app.state (目前未使用, 预留消息处理时访问 monitor/repo)。"""
        self._app_state = app_state

    def start(self) -> bool:
        """启动长连接线程。凭证不齐或已运行则跳过。返回是否真正启动。"""
        from app.services import preferences

        bot_id = preferences.get_wecom_bot_id()
        secret = preferences.get_wecom_bot_secret()
        if not bot_id or not secret:
            logger.info("智能机器人未启动: 缺少 BotID 或 Secret")
            return False
        with self._lock:
            if self._running:
                return False
            self._running = True
            self._last_error = ""
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("智能机器人长连接服务已启动 (bot_id=%s)", bot_id)
        return True

    def stop(self) -> None:
        """停止长连接线程。"""
        with self._lock:
            self._running = False
            loop = self._ws_loop
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            if not future.done():
                future.cancel()
        # 唤醒可能在 recv/退避 sleep 中的事件循环, 促使其退出
        if loop and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        self._ws_loop = None
        self._connected = False
        logger.info("智能机器人长连接服务已停止")

    def boot_check(self) -> None:
        """启动时检查 preferences, 凭证齐全且 enabled 则自动连接。

        失败静默降级(记 WARNING), 不阻断应用启动。
        """
        from app.services import preferences

        try:
            if preferences.get_wecom_bot_enabled():
                self.start()
        except Exception as e:  # noqa: BLE001
            logger.warning("智能机器人 boot_check 失败: %s", e)

    def apply_credential_change(self) -> None:
        """配置变更后重建连接。单连接限制: 必须 stop 再 start。

        保存新凭证 → stop 旧连接 → 若 enabled 且凭证齐全则 start 新连接。
        """
        was_running = self._running
        if was_running:
            self.stop()
        # 重新读取最新凭证判断是否应启动
        from app.services import preferences
        if preferences.get_wecom_bot_enabled() and self.start():
            logger.info("智能机器人凭证已更新, 重新连接")
        elif was_running:
            logger.info("智能机器人凭证已更新, 但当前未启用或凭证不齐, 停止连接")

    def status(self) -> dict:
        """返回连接状态(供 UI 展示)。"""
        from app.services import preferences
        return {
            "enabled": preferences.get_wecom_bot_enabled(),
            "running": self._running,
            "connected": self._connected,
            "bot_id_configured": bool(preferences.get_wecom_bot_id()),
            "secret_configured": bool(preferences.get_wecom_bot_secret()),
            "last_error": self._last_error,
        }

    # ================================================================
    # 主动推送 (aibot_send_msg)
    # ================================================================

    def send_text(
        self,
        chat_id: str,
        content: str,
        chat_type: int | None = None,
        *,
        timeout: float = 8.0,
    ) -> tuple[bool, str]:
        """通过已建立的长连接主动推送一条 Markdown 消息。

        官方文档 path/101463: 主动推送前, 用户需先在目标会话给机器人发过消息,
        否则服务端拿不到 chatid。响应通过 headers.req_id 关联。
        """
        chat_id = str(chat_id or "").strip()
        if not chat_id:
            return False, "未配置目标会话 (chat_id), 请先在 IM 管理选择会话"
        content = str(content or "").strip()
        if not content:
            return False, "消息内容为空"

        if chat_type is None:
            from app.services import preferences
            chat_type = preferences.get_wecom_bot_chat_type()

        with self._lock:
            ws = self._ws
            loop = self._ws_loop
            connected = self._connected
        if not connected or ws is None or loop is None:
            return False, "智能机器人长连接未连接, 请检查 BotID/Secret 与启用状态"

        req_id = str(uuid.uuid4())
        future: Future = Future()
        with self._lock:
            self._pending[req_id] = future
        body: dict = {
            "chatid": chat_id,
            "msgtype": "markdown",
            "markdown": {"content": content},
        }
        if chat_type in (0, 1, 2):
            body["chat_type"] = chat_type
        frame = {"cmd": "aibot_send_msg", "headers": {"req_id": req_id}, "body": body}

        try:
            loop.call_soon_threadsafe(self._async_send, ws, frame, req_id)
            try:
                resp = future.result(timeout=timeout)
            except TimeoutError:
                return False, f"主动推送超时 ({timeout:g}s), 请检查长连接状态"
            resp = resp if isinstance(resp, dict) else {}
            errcode = resp.get("errcode", (resp.get("body") or {}).get("errcode", -1))
            if errcode != 0:
                errmsg = resp.get("errmsg") or (resp.get("body") or {}).get("errmsg") or "未知错误"
                return False, f"企业微信拒绝消息 (errcode={errcode}): {errmsg}"
            return True, "ok"
        except Exception as exc:  # noqa: BLE001 - 推送失败只返回原因, 不抛给告警主流程
            return False, f"主动推送失败: {type(exc).__name__}: {exc}"
        finally:
            with self._lock:
                self._pending.pop(req_id, None)
            if not future.done():
                future.cancel()

    async def _async_send(self, ws, frame: dict, req_id: str) -> None:
        """在事件循环线程内发送帧, 异常透传给等待中的 Future。"""
        try:
            await ws.send(json.dumps(frame, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - 交给 send_text 的 Future 处理
            with self._lock:
                future = self._pending.get(req_id)
            if future is not None and not future.done():
                future.set_exception(exc)

    def _resolve_pending(self, frame: dict) -> None:
        """收到响应帧时唤醒对应 req_id 的 send_text 调用。"""
        headers = frame.get("headers")
        req_id = headers.get("req_id") if isinstance(headers, dict) else None
        if not req_id:
            return
        with self._lock:
            future = self._pending.pop(str(req_id), None)
        if future is not None and not future.done():
            future.set_result(frame)

    # ================================================================
    # 连接线程
    # ================================================================

    def _run_loop(self) -> None:
        """daemon 线程入口: 创建 asyncio 事件循环并运行连接主循环。"""
        try:
            loop = asyncio.new_event_loop()
            with self._lock:
                self._ws_loop = loop
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._connect_loop())
        except Exception as e:  # noqa: BLE001
            # stop() 的 loop.stop() 会以 "Event loop stopped before Future
            # completed" 打断 run_until_complete — 属预期停止路径, 不是故障。
            if not isinstance(e, RuntimeError) or "Event loop stopped" not in str(e):
                logger.warning("智能机器人连接线程异常: %s", e)
                self._last_error = str(e)
        finally:
            with self._lock:
                self._connected = False
                self._ws_loop = None
            try:
                # 取消残留任务并等其收尾 (websocket 上下文随之优雅关闭), 再关
                # 循环 — 否则连接任务被销毁时仍 pending, 日志噪音 + 连接靠
                # 服务端超时回收。loop.stop() 打断后循环未关闭, 可再次 run。
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    # 收尾设 5s 上限: 个别 task 吞取消/慢关 websocket 时不让 daemon
                    # 线程无限滞留 (join(10) 超时后仅剩日志误导)
                    loop.run_until_complete(
                        asyncio.wait_for(
                            asyncio.gather(*pending, return_exceptions=True), timeout=5.0
                        )
                    )
            except Exception:  # noqa: BLE001
                pass
            try:
                loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _connect_loop(self) -> None:
        """主连接循环: 连接 → 鉴权 → 心跳保活 → 断开 → 退避重连。"""
        import websockets

        from app.services import preferences

        attempt = 0
        while self._running:
            bot_id = preferences.get_wecom_bot_id()
            secret = preferences.get_wecom_bot_secret()
            if not bot_id or not secret:
                # 凭证被清空, 等待重新配置
                self._last_error = "缺少 BotID 或 Secret"
                await self._sleep_interruptible(5.0)
                continue

            try:
                async with websockets.connect(
                    _WECOM_WS_URL,
                    ping_interval=None,   # 用业务层 ping, 不用协议层
                    close_timeout=5,
                ) as ws:
                    # 1. 发送订阅鉴权帧
                    req_id = str(uuid.uuid4())
                    subscribe_frame = {
                        "cmd": "aibot_subscribe",
                        "headers": {"req_id": req_id},
                        "body": {"bot_id": bot_id, "secret": secret},
                    }
                    await ws.send(json.dumps(subscribe_frame))

                    # 2. 等待鉴权响应(errcode=0 表示成功)
                    resp_raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    resp = json.loads(resp_raw)
                    errcode = resp.get("errcode", resp.get("body", {}).get("errcode", -1))
                    if errcode != 0:
                        errmsg = resp.get("errmsg", resp.get("body", {}).get("errmsg", "未知错误"))
                        self._last_error = f"鉴权失败(errcode={errcode}): {errmsg}"
                        logger.warning("智能机器人鉴权失败: %s", self._last_error)
                        # 鉴权失败是凭证问题, 重连无益, 等待用户修正
                        await self._sleep_interruptible(30)
                        continue

                    # 连接成功
                    with self._lock:
                        self._connected = True
                        self._ws = ws
                    self._last_error = ""
                    attempt = 0
                    logger.info("智能机器人已连接 (bot_id=%s)", bot_id)

                    # 3. 心跳保活 + 接收循环
                    await self._maintain_connection(ws)

            except TimeoutError:
                self._last_error = "鉴权响应超时"
                logger.warning("智能机器人鉴权超时")
            except Exception as e:  # noqa: BLE001 — 网络/断开, 可重连
                self._last_error = str(e)
                logger.warning("智能机器人连接异常: %s", e)
            finally:
                with self._lock:
                    self._connected = False
                    self._ws = None

            # 4. 指数退避重连
            if not self._running:
                break
            attempt += 1
            delay = min(_RECONNECT_BASE_DELAY * (2 ** (attempt - 1)), _RECONNECT_CAP)
            logger.info("智能机器人 %ds 后重连(第 %d 次)", delay, attempt)
            await self._sleep_interruptible(delay)

    async def _maintain_connection(self, ws) -> None:
        """连接保持阶段: 每 30s 发 ping, 同时接收服务端推送。

        本阶段收到消息解析并记 INFO 日志(验证接收能力), 后续消息处理在此扩展。
        """
        while self._running:
            try:
                # 用 wait_for 同时实现"心跳定时"和"接收消息", 哪个先到都行
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=_HEARTBEAT_INTERVAL)
                    self._log_incoming(raw)
                    continue
                except TimeoutError:
                    pass  # 接收超时 → 到了心跳时间
                # 发送业务层 ping 保活
                await ws.send(json.dumps({"cmd": "ping"}))
            except Exception:
                raise

    def _log_incoming(self, raw) -> None:
        """解析收到的帧: 响应唤醒 Future、记录会话、记日志。"""
        try:
            frame = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.info("智能机器人收到非 JSON 消息: %s", str(raw)[:200])
            return
        if not isinstance(frame, dict):
            return
        # aibot_send_msg 等请求的响应按 req_id 关联, 没有 cmd
        self._resolve_pending(frame)
        cmd = frame.get("cmd", "?")
        body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
        # 长连接没有「拉取会话列表」接口 — 从回调里记住 chatid, 供主动推送选目标
        if cmd in ("aibot_msg_callback", "aibot_event_callback") and body.get("chatid"):
            from app.services import preferences
            try:
                preferences.remember_wecom_bot_chat(
                    str(body["chatid"]), str(body.get("chattype", ""))
                )
            except Exception as exc:  # noqa: BLE001 - 记录失败不影响消息收发
                logger.warning("记录智能机器人会话失败: %s", exc)
        # 用户消息: aibot_msg_callback (用户 @机器人 / 单聊发消息)
        if cmd == "aibot_msg_callback":
            userid = (body.get("from") or {}).get("userid", "?")
            chattype = body.get("chattype", "?")
            msgtype = body.get("msgtype", "?")
            # 文本消息内容在 body.text.content 或 body.content
            content = (body.get("text") or {}).get("content") or body.get("content", "")
            logger.info("智能机器人收到用户消息 [%s/%s] %s: %s",
                        chattype, msgtype, userid, str(content)[:100])
        # 事件回调: 进入会话 / 卡片点击 / 连接被踢等
        elif cmd == "aibot_event_callback":
            eventtype = body.get("event", {}).get("eventtype", "?")
            logger.info("智能机器人收到事件回调: %s", eventtype)
        else:
            logger.info("智能机器人收到帧 cmd=%s: %s", cmd, str(raw)[:200])

    async def _sleep_interruptible(self, seconds: float) -> None:
        """可被 stop() 中断的 sleep(通过检查 _running)。"""
        waited = 0.0
        while self._running and waited < seconds:
            await asyncio.sleep(min(0.5, seconds - waited))
            waited += 0.5
