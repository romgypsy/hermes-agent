"""
LINE platform adapter.

Uses line-bot-sdk library for:
- Receiving messages from users/groups
- Sending responses back
- Handling media and commands
"""

import asyncio
import json
import logging
import mimetypes
import os
import re
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

try:
    from linebot import (
        WebhookHandler,
        WebhookParser,
    )
    from linebot.models import (
        MessageEvent,
        TextMessage,
        ImageMessage,
        ImageSendMessage,
        VideoMessage,
        AudioMessage,
        LocationMessage,
        StickerMessage,
    )
    from linebot import LineBotApi
    from linebot.exceptions import InvalidSignatureError
    LINE_AVAILABLE = True
except ImportError:
    LINE_AVAILABLE = False
    WebhookHandler = Any
    WebhookParser = Any
    InvalidSignatureError = Exception
    MessageEvent = Any
    TextMessage = Any
    ImageMessage = Any
    ImageSendMessage = Any
    VideoMessage = Any
    AudioMessage = Any
    LocationMessage = Any
    StickerMessage = Any
    LineBotApi = Any

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent as HermesMessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)
from gateway.session import build_session_key
from gateway.line_stock_chart import (
    build_stock_chart_for_command,
    chart_output_dir,
    chart_public_base_url,
    parse_stock_chart_command,
)


def check_line_requirements() -> bool:
    """Check if LINE dependencies are available."""
    return LINE_AVAILABLE


class LineAdapter(BasePlatformAdapter):
    """
    LINE bot adapter.
    
    Handles:
    - Receiving messages from users and groups
    - Sending responses
    - Webhook-based message delivery (LINE doesn't support polling)
    """
    
    # LINE Messaging API limits: max 5,000 characters per text message and
    # up to 5 message objects per reply/push request.  Always split or cap
    # outbound text before creating TextMessage objects so users do not get
    # silently truncated/incomplete responses from LINE.
    MAX_MESSAGE_LENGTH = 5000
    MAX_TEXT_MESSAGES_PER_SEND = 5
    _CONTINUATION_HEADER_BUDGET = 80
    
    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.LINE)
        
        # LINE credentials from config.extra or environment
        self._channel_access_token = self.config.extra.get('channel_access_token') or \
                                     os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '')
        self._channel_secret = self.config.extra.get('channel_secret') or \
                               os.getenv('LINE_CHANNEL_SECRET', '')
        
        # Initialize LINE client
        self._line_bot_api: Optional[LineBotApi] = None
        self._webhook_handler: Optional[WebhookHandler] = None
        
        # Keep latest reply token per chat so we can reply even if gateway
        # send path doesn't forward platform-specific metadata.
        self._latest_reply_token_by_chat: Dict[str, str] = {}

        # LINE cannot send a photo and a text caption in the same user message.
        # When the user sends a photo by itself, keep the downloaded media here,
        # ask what they want to know, then attach the media to the next text turn.
        self._pending_line_photo_events: Dict[str, HermesMessageEvent] = {}
        
        logger.info("[%s] LINE adapter initialized (webhook mode)", self.name)
    
    @property
    def has_async_delivery(self) -> bool:
        """LINE uses webhook-based async delivery."""
        return True
    
    async def start(self) -> None:
        """Start the LINE adapter."""
        if not LINE_AVAILABLE:
            logger.error("[%s] line-bot-sdk not installed. Install with: pip install line-bot-sdk", self.name)
            return
        
        if not self._channel_access_token or not self._channel_secret:
            logger.error("[%s] Missing LINE credentials. Set LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET.", self.name)
            return
        
        # Initialize LINE client
        try:
            self._line_bot_api = LineBotApi(self._channel_access_token)
            self._webhook_handler = WebhookHandler(self._channel_secret)
            logger.info("[%s] LINE bot client initialized successfully", self.name)
        except Exception as e:
            logger.error("[%s] Failed to initialize LINE bot client: %s", self.name, e, exc_info=True)
            raise
    
    async def stop(self) -> None:
        """Stop the LINE adapter."""
        logger.info("[%s] Stopping LINE adapter", self.name)
        # LINE doesn't need to stop polling - it's webhook-based
        self._line_bot_api = None
        self._webhook_handler = None
    
    async def receive(self) -> Optional[HermesMessageEvent]:
        """LINE uses webhook callbacks that dispatch directly via handle_message()."""
        await asyncio.sleep(0.1)
        return None

    def _line_session_key(self, event: HermesMessageEvent) -> str:
        """Build the same session key BasePlatformAdapter uses for LINE events."""
        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
        )

    @staticmethod
    def _metadata_paths(metadata: Optional[Dict[str, Any]]) -> List[str]:
        paths: List[str] = []
        if not isinstance(metadata, dict):
            return paths
        for key in ("temp_media_paths", "line_temp_media_paths"):
            value = metadata.get(key)
            if isinstance(value, (list, tuple, set)):
                paths.extend(str(p) for p in value if p)
            elif isinstance(value, str) and value:
                paths.append(value)
        return list(dict.fromkeys(paths))

    def _merge_pending_line_photo(self, session_key: str, event: HermesMessageEvent) -> None:
        """Store/merge a LINE photo while waiting for the user's text question."""
        existing = self._pending_line_photo_events.get(session_key)
        if existing is None:
            self._pending_line_photo_events[session_key] = event
            return
        existing.media_urls.extend(event.media_urls or [])
        existing.media_types.extend(event.media_types or [])
        if event.text:
            existing.text = self._merge_caption(existing.text, event.text)
        existing_paths = self._metadata_paths(getattr(existing, "metadata", None))
        incoming_paths = self._metadata_paths(getattr(event, "metadata", None))
        merged_paths = list(dict.fromkeys(existing_paths + incoming_paths))
        if not isinstance(getattr(existing, "metadata", None), dict):
            existing.metadata = {}
        if merged_paths:
            existing.metadata["line_temp_media_paths"] = merged_paths
            existing.metadata["temp_media_paths"] = merged_paths

    def _attach_pending_line_photo(self, session_key: str, event: HermesMessageEvent) -> bool:
        """Attach a previously received LINE photo to the current text event."""
        pending = self._pending_line_photo_events.pop(session_key, None)
        if pending is None:
            return False
        event.media_urls = list(pending.media_urls or []) + list(event.media_urls or [])
        event.media_types = list(pending.media_types or []) + list(event.media_types or [])
        if event.media_urls:
            event.message_type = MessageType.PHOTO
        if not isinstance(getattr(event, "metadata", None), dict):
            event.metadata = {}
        pending_paths = self._metadata_paths(getattr(pending, "metadata", None))
        event_paths = self._metadata_paths(getattr(event, "metadata", None))
        merged_paths = list(dict.fromkeys(pending_paths + event_paths))
        if merged_paths:
            event.metadata["line_temp_media_paths"] = merged_paths
            event.metadata["temp_media_paths"] = merged_paths
        event.metadata["line_pending_photo_attached"] = True
        return True

    async def handle_message(self, event: HermesMessageEvent) -> None:
        """Handle LINE's photo-then-text UX before entering the generic pipeline.

        LINE users cannot send image + text caption in the same message.  A bare
        photo should therefore receive a short prompt and wait for the next text
        message.  The next normal text message is processed with the stored
        photo attached, and the existing base cleanup deletes the temp image
        after Hermes sends the answer.
        """
        try:
            session_key = self._line_session_key(event)
            is_photo = event.message_type == MessageType.PHOTO
            has_media = bool(getattr(event, "media_urls", None))
            has_text = bool((event.text or "").strip())

            if event.message_type == MessageType.TEXT and has_text:
                chart_command = parse_stock_chart_command(event.text)
                if chart_command is not None:
                    try:
                        chart_result = build_stock_chart_for_command(
                            event.text,
                            public_base_url=(
                                self.config.extra.get('chart_public_base_url')
                                if isinstance(getattr(self.config, 'extra', None), dict)
                                else None
                            ),
                        )
                    except Exception as exc:
                        logger.warning("[%s] LINE stock chart generation failed: %s", self.name, exc)
                        await self.send(
                            event.source.chat_id,
                            "ขออภัยครับ ค้นหา symbol และสร้างกราฟไม่สำเร็จในตอนนี้ กรุณาลองระบุ symbol พร้อมตลาด/ตลาดหลักทรัพย์ เช่น ASML, ASML.AS, TSM, 7203.T, AOT.BK หรือ BTC",
                            reply_to=event.message_id,
                            metadata=getattr(event, "metadata", None),
                        )
                        return
                    if chart_result is not None:
                        await self.send_image(
                            event.source.chat_id,
                            chart_result.image_url,
                            caption=chart_result.caption,
                            reply_to=event.message_id,
                            metadata=getattr(event, "metadata", None),
                        )
                        logger.warning(
                            "[%s] Sent LINE stock chart %s (%s) to %s: %s",
                            self.name,
                            chart_result.symbol,
                            chart_result.period,
                            event.source.chat_id,
                            chart_result.image_url,
                        )
                        return

            if is_photo and has_media and not has_text:
                self._merge_pending_line_photo(session_key, event)
                prompt = "ได้รับรูปแล้วครับ คุณต้องการสอบถามอะไรเกี่ยวกับรูปนี้ครับ?"
                await self.send(
                    event.source.chat_id,
                    prompt,
                    reply_to=event.message_id,
                    metadata=getattr(event, "metadata", None),
                )
                logger.warning(
                    "[%s] Stored LINE photo for session %s and prompted for a follow-up question",
                    self.name,
                    session_key,
                )
                return

            if event.message_type == MessageType.TEXT and has_text and not event.is_command():
                if self._attach_pending_line_photo(session_key, event):
                    logger.warning(
                        "[%s] Attached pending LINE photo to follow-up text for session %s",
                        self.name,
                        session_key,
                    )
        except Exception as exc:
            logger.warning("[%s] LINE pending-photo pre-processing failed: %s", self.name, exc)

        await super().handle_message(event)

    async def _process_message_background(self, event: HermesMessageEvent, session_key: str) -> None:
        """Process LINE messages and delete downloaded temporary media afterward."""
        try:
            await super()._process_message_background(event, session_key)
        finally:
            for path in self._metadata_paths(getattr(event, "metadata", None)):
                try:
                    p = Path(path)
                    if p.exists() and p.is_file():
                        p.unlink()
                        logger.debug("[%s] Deleted temporary LINE media %s", self.name, path)
                except OSError as exc:
                    logger.debug("[%s] Failed to delete temporary LINE media %s: %s", self.name, path, exc)
     
    @classmethod
    def _split_text_for_line(cls, text: str) -> List[str]:
        """Split/cap text so every LINE TextMessage is <= 5,000 chars.

        LINE accepts at most 5 message objects in a single reply/push request.
        We preserve as much content as safely possible and add a Thai truncation
        note if the response still exceeds what LINE can deliver in one send.
        """
        text = str(text or "")
        if len(text) <= cls.MAX_MESSAGE_LENGTH:
            return [text]

        body_limit = cls.MAX_MESSAGE_LENGTH - cls._CONTINUATION_HEADER_BUDGET
        chunks: List[str] = []
        remaining = text.strip()

        while remaining and len(chunks) < cls.MAX_TEXT_MESSAGES_PER_SEND:
            if len(remaining) <= body_limit:
                piece = remaining
                remaining = ""
            else:
                newline_cut = remaining.rfind("\n", 0, body_limit)
                space_cut = remaining.rfind(" ", 0, body_limit)
                cut = max(newline_cut, space_cut)
                if cut < int(body_limit * 0.60):
                    cut = body_limit
                piece = remaining[:cut].rstrip()
                remaining = remaining[cut:].lstrip()
            chunks.append(piece)

        if remaining and chunks:
            note = "\n\n[ข้อความยาวเกินข้อจำกัด LINE จึงตัดให้พอดี กรุณาพิมพ์ 'ต่อ' หรือขอให้สรุปเพิ่มได้ครับ]"
            max_body = body_limit - len(note)
            chunks[-1] = chunks[-1][:max_body].rstrip() + note

        total = len(chunks)
        if total <= 1:
            return chunks

        framed: List[str] = []
        for idx, chunk in enumerate(chunks, start=1):
            header = f"[ส่วน {idx}/{total}]\n"
            framed.append(header + chunk[: cls.MAX_MESSAGE_LENGTH - len(header)])
        return framed

    @classmethod
    def _build_text_messages(cls, text: str) -> List[TextMessage]:
        """Build LINE TextMessage objects, enforcing LINE text limits first."""
        return [TextMessage(text=part) for part in cls._split_text_for_line(text)]

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """
        Send a message to LINE.
        
        Args:
            content: Message content (plaintext)
            chat_id: LINE user/group/room ID (reply_token or user_id)
            metadata: Optional metadata (reply_token for reply mode)
        
        Returns:
            SendResult with success status
        """
        if self.has_fatal_error:
            return SendResult(success=False, error="Adapter in fatal error state")
        
        if not self._line_bot_api:
            return SendResult(success=False, error="LINE bot client not initialized")
        
        # Check if this is a reply (has reply_token) or push (direct send).
        # Some gateway-side status messages (for example immediate "processing"
        # acknowledgements) must not consume LINE's one-time reply token, so
        # callers can force push mode with metadata['line_force_push'] or
        # metadata['skip_reply_token'].
        force_push = bool(metadata and (metadata.get('line_force_push') or metadata.get('skip_reply_token')))
        reply_token = None if force_push else (metadata.get('reply_token') if metadata else None)
        if not reply_token and not force_push:
            reply_token = self._latest_reply_token_by_chat.pop(str(chat_id), None)
        
        try:
            # LINE message formatting - plain text only (no markdown support)
            formatted_content = self.format_message(content)
            text_messages = self._build_text_messages(formatted_content)
            
            if reply_token:
                # Reply mode (respond to specific message)
                try:
                    self._line_bot_api.reply_message(
                        reply_token,
                        text_messages,
                    )
                except Exception as e:
                    logger.warning("[%s] Reply failed, falling back to push: %s", self.name, e)
                    self._line_bot_api.push_message(
                        chat_id,
                        text_messages,
                    )
            else:
                # Push mode (send directly without receiving a message)
                # Use channel access token for push messages
                # Note: LINE requires "Messaging API" plan for push messages
                try:
                    # Try push message
                    self._line_bot_api.push_message(
                        chat_id,
                        text_messages,
                    )
                except Exception as e:
                    # If push fails, it might be due to API plan limitations
                    logger.warning("[%s] Push message failed (requires Messaging API plan): %s", self.name, e)
                    logger.warning("[%s] Messages can only be sent as replies to received messages", self.name)
                    return SendResult(success=False, error=f"Push not supported: {e}")
            
            send_mode = "reply" if reply_token else "push"
            logger.warning(
                "[%s] Sent message successfully via %s to %s (length=%d, parts=%d)",
                self.name,
                send_mode,
                chat_id,
                len(formatted_content),
                len(text_messages),
            )
            return SendResult(success=True, message_id=chat_id)
            
        except Exception as e:
            logger.error("[%s] Failed to send message to %s: %s", self.name, chat_id, e, exc_info=True)
            return SendResult(success=False, error=str(e))
    
    @staticmethod
    def _format_image_caption(caption: Optional[str], image_url: str) -> str:
        """Build LINE image companion text without exposing the image URL.

        LINE sends the image as a native image bubble, so the companion text
        should contain only analysis/caption text.  If upstream content already
        included the public chart URL or old chart-view URL as a fallback, strip
        those lines before sending to users.
        """
        if not caption:
            return ""
        image_url = str(image_url or "").strip()
        text = str(caption).strip()
        if image_url:
            text = text.replace(image_url, "")
        text = re.sub(r"https://\S+/chart-view/\S+", "", text)
        text = re.sub(r"https://\S+/chart-embed/\S+", "", text)
        text = re.sub(r"https://\S+/charts/\S+", "", text)
        cleaned_lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                if cleaned_lines and cleaned_lines[-1] != "":
                    cleaned_lines.append("")
                continue
            if stripped.startswith(("เปิดหน้าดูรูป:", "ไฟล์ PNG โดยตรง:", "URL:", "Image URL:", "LINE_IMAGE_URL:")):
                continue
            cleaned_lines.append(stripped)
        return "\n".join(cleaned_lines).strip()[:LineAdapter.MAX_MESSAGE_LENGTH]

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a public HTTPS image URL as a native LINE image message."""
        if self.has_fatal_error:
            return SendResult(success=False, error="Adapter in fatal error state")
        if not self._line_bot_api:
            return SendResult(success=False, error="LINE bot client not initialized")
        if not str(image_url).startswith("https://"):
            return SendResult(success=False, error="LINE image URLs must be public HTTPS URLs")

        force_push = bool(metadata and (metadata.get('line_force_push') or metadata.get('skip_reply_token')))
        reply_token = None if force_push else (metadata.get('reply_token') if metadata else None)
        if not reply_token and not force_push:
            reply_token = self._latest_reply_token_by_chat.pop(str(chat_id), None)

        messages = [ImageSendMessage(original_content_url=image_url, preview_image_url=image_url)]
        companion_text = self._format_image_caption(caption, image_url)
        if companion_text:
            messages.append(TextMessage(text=self.format_message(companion_text)[:self.MAX_MESSAGE_LENGTH]))

        try:
            if reply_token:
                try:
                    self._line_bot_api.reply_message(reply_token, messages)
                except Exception as e:
                    logger.warning("[%s] Image reply failed, falling back to push: %s", self.name, e)
                    self._line_bot_api.push_message(chat_id, messages)
            else:
                self._line_bot_api.push_message(chat_id, messages)
            logger.warning("[%s] Sent image successfully to %s: %s", self.name, chat_id, image_url)
            return SendResult(success=True, message_id=chat_id)
        except Exception as e:
            logger.error("[%s] Failed to send image to %s: %s", self.name, chat_id, e, exc_info=True)
            return SendResult(success=False, error=str(e))

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        """Publish a local image file to LINE's public chart host and send it natively.

        LINE cannot fetch local paths such as /tmp/hermes-*.png.  Generic Hermes
        media delivery may hand us a local path from MEDIA: tags or bare path
        extraction, so bridge that path into /var/www/hermes/charts and convert it
        to https://img.clyfe.online/charts/<file> before calling send_image().
        """
        try:
            source = Path(str(image_path)).expanduser().resolve()
            if not source.is_file():
                return SendResult(success=False, error=f"Image file not found: {image_path}")
            if source.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                return SendResult(success=False, error=f"Unsupported image file type: {source.suffix}")

            outdir = chart_output_dir().resolve()
            safe_stem = re.sub(r"[^A-Za-z0-9_.=-]+", "_", source.stem).strip("._") or "line_image"
            filename = f"{safe_stem}_{uuid.uuid4().hex[:8]}{source.suffix.lower()}"
            dest = outdir / filename
            dest.write_bytes(source.read_bytes())
            try:
                dest.chmod(0o644)
            except OSError:
                pass

            configured_base = (
                self.config.extra.get('chart_public_base_url')
                if isinstance(getattr(self.config, 'extra', None), dict)
                else None
            )
            image_url = chart_public_base_url(configured_base) + filename
            logger.warning(
                "[%s] Published local LINE image %s -> %s",
                self.name,
                source,
                image_url,
            )
            return await self.send_image(
                chat_id=chat_id,
                image_url=image_url,
                caption=caption,
                reply_to=reply_to,
                metadata=metadata,
            )
        except Exception as e:
            logger.error("[%s] Failed to publish/send local image %s: %s", self.name, image_path, e, exc_info=True)
            return SendResult(success=False, error=str(e))

    def format_message(self, content: str) -> str:
        """
        Format message for LINE.
        
        LINE doesn't support markdown - plain text only.
        Strip any markdown formatting for better readability.
        """
        if not content:
            return content
        
        # Remove common markdown syntax
        text = content
        
        # Remove code blocks
        text = text.replace('```', '')
        
        # Remove inline code
        text = text.replace('`', '')
        
        # Remove bold markers (**bold** -> bold)
        import re
        text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
        
        # Remove italic markers (*italic* -> italic)
        text = re.sub(r'\*([^*]+)\*', r'\1', text)
        
        # Remove strikethrough (~~text~~ -> text)
        text = re.sub(r'~~([^~]+)~~', r'\1', text)
        
        # Remove link syntax [text](url) -> text (url)
        text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
        
        return text
    
    def _line_media_dir(self) -> Path:
        """Return the local folder used for transient LINE media downloads."""
        configured = (
            self.config.extra.get('media_dir')
            if isinstance(getattr(self.config, 'extra', None), dict)
            else None
        ) or os.getenv('LINE_MEDIA_DIR')
        if configured:
            media_dir = Path(configured).expanduser()
        else:
            media_dir = Path(os.getenv('HERMES_HOME', '~/.hermes')).expanduser() / 'cache' / 'line-media'
        media_dir.mkdir(parents=True, exist_ok=True)
        return media_dir

    @staticmethod
    def _extension_for_content_type(content_type: str, default: str = '.jpg') -> str:
        content_type = (content_type or '').split(';', 1)[0].strip().lower()
        if content_type == 'image/jpeg':
            return '.jpg'
        if content_type == 'image/png':
            return '.png'
        if content_type == 'image/gif':
            return '.gif'
        if content_type == 'image/webp':
            return '.webp'
        guessed = mimetypes.guess_extension(content_type) if content_type else None
        return guessed or default

    def _download_line_message_content(self, message_id: str, *, default_ext: str = '.jpg') -> tuple[Optional[str], Optional[str]]:
        """Download LINE message content by message id and return (local_path, mime_type).

        LINE image/video/audio webhook events contain a message id, not a
        public URL.  The binary must be fetched from the Messaging API content
        endpoint via line-bot-sdk's get_message_content().
        """
        if not self._line_bot_api:
            logger.warning("[%s] Cannot download LINE media %s: API client not initialized", self.name, message_id)
            return None, None
        safe_message_id = re.sub(r'[^A-Za-z0-9_.-]+', '_', str(message_id or 'media'))[:120] or 'media'
        try:
            response = self._line_bot_api.get_message_content(str(message_id))
            headers = getattr(response, 'headers', {}) or {}
            content_type = (
                headers.get('Content-Type')
                or headers.get('content-type')
                or 'image/jpeg'
            )
            ext = self._extension_for_content_type(content_type, default_ext)
            dest = self._line_media_dir() / f"{safe_message_id}_{uuid.uuid4().hex[:8]}{ext}"
            with open(dest, 'wb') as f:
                if hasattr(response, 'iter_content'):
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                else:
                    data = getattr(response, 'content', b'')
                    if isinstance(data, str):
                        data = data.encode('utf-8')
                    f.write(data)
            logger.warning("[%s] Downloaded LINE media %s to %s", self.name, message_id, dest)
            return str(dest), content_type.split(';', 1)[0].strip().lower() or None
        except Exception as e:
            logger.error("[%s] Failed to download LINE media %s: %s", self.name, message_id, e, exc_info=True)
            return None, None

    def handle_webhook_event(
        self,
        body: str,
        signature: str
    ) -> List[HermesMessageEvent]:
        """
        Handle LINE webhook event.
        
        This method is called by the FastAPI endpoint that receives LINE webhooks.
        
        Args:
            body: Raw request body
            signature: X-Line-Signature header
        
        Returns:
            List of HermesMessageEvent objects
        
        Raises:
            InvalidSignatureError: If signature verification fails
        """
        if not self._webhook_handler:
            logger.error("[%s] Webhook handler not initialized", self.name)
            raise InvalidSignatureError("Webhook handler not initialized")
        
        # Parse events with LINE SDK parser.
        # This call also validates X-Line-Signature with the channel secret.
        try:
            parser = WebhookParser(self._channel_secret)
            events = parser.parse(body, signature)
        except InvalidSignatureError:
            logger.error("[%s] Invalid webhook signature", self.name)
            raise
        except Exception as e:
            logger.error("[%s] Failed to parse webhook body: %s", self.name, e, exc_info=True)
            # Verification requests may carry empty events/body variants; treat as no events.
            return []
        
        # Convert LINE events to Hermes message events
        hermes_events = []
        
        for event in events:
            if not isinstance(event, MessageEvent):
                continue
            
            # Extract message type and content
            message_type = MessageType.TEXT
            text_content = None
            media_url = None
            media_mime_type = None
            temp_media_paths: List[str] = []
            
            if isinstance(event.message, TextMessage):
                message_type = MessageType.TEXT
                text_content = event.message.text
            elif isinstance(event.message, ImageMessage):
                message_type = MessageType.PHOTO
                message_id = str(event.message.id) if hasattr(event.message, 'id') else ''
                media_url, media_mime_type = self._download_line_message_content(
                    message_id,
                    default_ext='.jpg',
                )
                if media_url:
                    temp_media_paths.append(media_url)
            elif isinstance(event.message, VideoMessage):
                message_type = MessageType.VIDEO
                media_url = event.message.original_content_url
            elif isinstance(event.message, AudioMessage):
                message_type = MessageType.AUDIO
                media_url = event.message.original_content_url
            elif isinstance(event.message, LocationMessage):
                message_type = MessageType.LOCATION
                text_content = f"Location: {event.message.title}"
            elif isinstance(event.message, StickerMessage):
                message_type = MessageType.TEXT
                text_content = "[Sticker]"
            else:
                # Unknown message type, skip
                continue
            
            # Determine source (user, group, room)
            source = event.source
            if hasattr(source, 'group_id') and source.group_id:
                source_type = 'group'
                chat_id = source.group_id
            elif hasattr(source, 'room_id') and source.room_id:
                source_type = 'room'
                chat_id = source.room_id
            elif hasattr(source, 'user_id') and source.user_id:
                source_type = 'user'
                chat_id = source.user_id
            else:
                logger.warning("[%s] Unknown event source: %s", self.name, source)
                continue
            
            # Build normalized source + MessageEvent for gateway core
            source_obj = self.build_source(
                chat_id=str(chat_id),
                chat_type='group' if source_type in ('group', 'room') else 'dm',
                user_id=str(source.user_id) if hasattr(source, 'user_id') and source.user_id else None,
            )

            hermes_event = HermesMessageEvent(
                text=text_content or '',
                message_type=message_type,
                source=source_obj,
                raw_message=event,
                message_id=str(event.message.id) if hasattr(event.message, 'id') else None,
            )
            if media_url:
                hermes_event.media_urls = [media_url]
                hermes_event.media_types = [media_mime_type or message_type.value]

            # Preserve reply token for LINE reply API (consumed by gateway send path)
            reply_token = getattr(event, 'reply_token', None)
            if reply_token:
                self._latest_reply_token_by_chat[str(chat_id)] = str(reply_token)
            hermes_event.metadata = {
                'reply_token': reply_token,
                'event_type': getattr(event, 'type', None),
                'message_id': str(event.message.id) if hasattr(event.message, 'id') else None,
            }
            if temp_media_paths:
                hermes_event.metadata['line_temp_media_paths'] = list(temp_media_paths)
                hermes_event.metadata['temp_media_paths'] = list(temp_media_paths)
            
            hermes_events.append(hermes_event)
            logger.info(
                "[%s] Received LINE %s from %s (user=%s): %s",
                self.name,
                message_type.value,
                chat_id,
                source.user_id if hasattr(source, 'user_id') else 'N/A',
                text_content[:50] if text_content else '(media)',
            )
        
        return hermes_events
    
    async def connect(self) -> bool:
        """
        Connect to LINE platform.
        
        LINE doesn't require persistent connections like Telegram or Discord.
        Instead, we verify credentials and set up the webhook receiver.
        """
        try:
            # Validate credentials by attempting to build the handler
            if not self._channel_access_token or not self._channel_secret:
                logger.error("[%s] LINE credentials not configured", self.name)
                self.has_fatal_error = True
                self.fatal_error_code = "MISSING_CREDENTIALS"
                self.fatal_error_message = "LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET must be set"
                return False
            
            logger.info("[%s] ✓ LINE credentials validated", self.name)

            # Initialize LINE API client + webhook handler used for signature/event parsing
            try:
                self._line_bot_api = LineBotApi(self._channel_access_token)
                self._webhook_handler = WebhookHandler(self._channel_secret)
                logger.info("[%s] ✓ LINE SDK client initialized", self.name)
            except Exception as e:
                logger.error("[%s] Failed to initialize LINE SDK client: %s", self.name, e, exc_info=True)
                self.has_fatal_error = True
                self.fatal_error_code = "INIT_FAILED"
                self.fatal_error_message = f"LINE SDK init failed: {e}"
                return False
            
            # Set up LINE webhook receiver.
            # NOTE: PlatformConfig has no top-level `webhook_url` attribute, so
            # `hasattr(self.config, 'webhook_url')` is always False and the server
            # never starts. This made LINE appear "connected" while inbound
            # messages never arrived. Start internal webhook server by default
            # unless explicitly disabled.
            disable_internal_webhook = os.getenv("LINE_DISABLE_INTERNAL_WEBHOOK", "").strip().lower() in {
                "1", "true", "yes", "on"
            }

            if disable_internal_webhook:
                logger.info(
                    "[%s] Internal LINE webhook server disabled by LINE_DISABLE_INTERNAL_WEBHOOK.",
                    self.name,
                )
            else:
                webhook_url = (
                    (self.config.extra.get("webhook_url") if isinstance(getattr(self.config, "extra", None), dict) else None)
                    or os.getenv("LINE_WEBHOOK_URL")
                    or "https://hermes.clyfe.online/webhook"
                )
                try:
                    self._setup_line_webhook()
                    logger.info("[%s] LINE webhook active at %s", self.name, webhook_url)
                except Exception as e:
                    logger.warning(
                        "[%s] Failed to start LINE webhook server: %s. "
                        "Gateway will still send messages via LINE API, "
                        "but inbound webhook notifications will fail.",
                        self.name,
                        e,
                    )

            return True
            
        except Exception as e:
            logger.error("[%s] Connection failed: %s", self.name, e, exc_info=True)
            self.has_fatal_error = True
            self.fatal_error_code = "CONNECTION_FAILED"
            self.fatal_error_message = str(e)
            return False
    
    def _setup_line_webhook(self):
        """Start HTTP server for LINE webhook callbacks."""
        from gateway.line_webhook import create_line_webhook_app
        
        # Create FastAPI app for LINE webhook
        app = create_line_webhook_app(self)
        
        # Determine host and port from config or env
        # Cloudflare proxy requires port 80 (not 8080!)
        webhook_host = os.getenv("LINE_SERVER_HOST", "0.0.0.0")
        webhook_port = int(os.getenv("LINE_SERVER_PORT", "80"))
        
        # Start uvicorn server in background
        def run_server():
            import uvicorn
            uvicorn.run(
                app,
                host=webhook_host,
                port=webhook_port,
                log_level="info",
                access_log=False,
                loop="asyncio"
            )
        
        import threading
        self._webhook_thread = threading.Thread(
            target=run_server,
            daemon=True,
            name="LINE-webhook"
        )
        self._webhook_thread.start()
        logger.info(
            "[%s] LINE webhook server starting on %s:%s", 
            self.name, webhook_host, webhook_port
        )

    async def disconnect(self) -> None:
        """Disconnect the LINE webhook server."""
        # LINE uses webhook, no active connection to disconnect
        logger.info("[%s] LINE webhook server stopped", self.name)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get information about a LINE chat."""
        # LINE doesn't have a way to fetch chat info via API
        return {
            "name": f"LINE-{chat_id}",
            "type": "dm"
        }