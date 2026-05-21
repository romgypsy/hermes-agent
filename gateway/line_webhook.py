"""
FastAPI endpoint for LINE webhook integration.

This module provides a FastAPI endpoint that receives LINE webhooks
and forwards events to the Hermes LINE adapter.
"""

import base64
import html
import logging
from urllib.parse import quote

from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from gateway.line_stock_chart import chart_output_dir

logger = logging.getLogger(__name__)
__all__ = ["create_line_webhook_app"]

# Global reference to LINE adapter (set by gateway runner)
_line_adapter = None


def set_line_adapter(adapter):
    """Set the global LINE adapter instance."""
    global _line_adapter
    _line_adapter = adapter


def create_line_webhook_app(adapter):
    """
    Create FastAPI app for LINE webhook.
    
    Args:
        adapter: LineAdapter instance from Hermes gateway
    
    Returns:
        FastAPI application
    """
    app = FastAPI(
        title="LINE Webhook Endpoint",
        description="LINE webhook integration for Hermes Agent",
        version="1.0.0"
    )
    
    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )
    
    # Store adapter reference
    set_line_adapter(adapter)

    @app.get("/charts/{filename:path}")
    async def chart_png(filename: str):
        """Serve generated chart PNGs with browser/LINE-safe explicit headers."""
        safe_name = filename.strip().lstrip("/")
        if "/" in safe_name or "\\" in safe_name or not safe_name.lower().endswith(".png"):
            raise HTTPException(status_code=404, detail="Chart not found")
        chart_path = chart_output_dir() / safe_name
        if not chart_path.is_file():
            raise HTTPException(status_code=404, detail="Chart not found")
        return FileResponse(
            chart_path,
            media_type="image/png",
            filename=safe_name,
            content_disposition_type="inline",
            headers={
                "Cache-Control": "no-store, max-age=0, no-transform",
                "X-Content-Type-Options": "nosniff",
                "Cross-Origin-Resource-Policy": "cross-origin",
                "Access-Control-Allow-Origin": "*",
            },
        )

    @app.get("/chart-embed/{filename:path}", response_class=HTMLResponse)
    async def chart_embed(filename: str):
        """Self-contained diagnostic chart page with the PNG embedded as base64."""
        safe_name = filename.strip().lstrip("/")
        if "/" in safe_name or "\\" in safe_name or not safe_name.lower().endswith(".png"):
            raise HTTPException(status_code=404, detail="Chart not found")
        chart_path = chart_output_dir() / safe_name
        if not chart_path.is_file():
            raise HTTPException(status_code=404, detail="Chart not found")
        png_bytes = chart_path.read_bytes()
        encoded_png = base64.b64encode(png_bytes).decode("ascii")
        escaped_name = html.escape(safe_name)
        raw_url = "/charts/" + quote(safe_name)
        view_url = "/chart-view/" + quote(safe_name)
        return HTMLResponse(
            content=f"""<!doctype html>
<html lang=\"th\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>Embedded {escaped_name}</title>
  <style>
    body {{ margin: 0; background: #111827; color: #f9fafb; font-family: system-ui, -apple-system, Segoe UI, sans-serif; }}
    main {{ padding: 16px; }}
    img {{ display: block; width: min(100%, 1200px); height: auto; margin: 12px auto; background: white; border-radius: 8px; }}
    code {{ background: #1f2937; padding: 2px 6px; border-radius: 4px; }}
    a {{ color: #93c5fd; word-break: break-all; }}
  </style>
</head>
<body>
  <main>
    <h1>{escaped_name}</h1>
    <p>Diagnostic embedded image page. PNG bytes: {len(png_bytes)}</p>
    <p>Raw PNG: <a href=\"{raw_url}\">{raw_url}</a></p>
    <p>HTML viewer: <a href=\"{view_url}\">{view_url}</a></p>
    <img src=\"data:image/png;base64,{encoded_png}\" alt=\"{escaped_name}\">
  </main>
</body>
</html>""",
            headers={
                "Cache-Control": "no-store, max-age=0",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/chart-view/{filename:path}", response_class=HTMLResponse)
    async def chart_view(filename: str):
        """Browser-friendly HTML wrapper for generated chart PNGs.

        Some mobile/in-app browsers show a spinner on direct image documents even
        though the PNG request returns 200.  This route wraps the same PNG in a
        normal HTML page so Chrome/LINE has a document to render while preserving
        the raw /charts/<file>.png endpoint required by LINE image messages.
        """
        safe_name = filename.strip().lstrip("/")
        if "/" in safe_name or "\\" in safe_name or not safe_name.lower().endswith(".png"):
            raise HTTPException(status_code=404, detail="Chart not found")
        chart_path = chart_output_dir() / safe_name
        if not chart_path.is_file():
            raise HTTPException(status_code=404, detail="Chart not found")

        escaped_name = html.escape(safe_name)
        image_src = "/charts/" + quote(safe_name)
        direct_url = image_src
        return HTMLResponse(
            content=f"""<!doctype html>
<html lang=\"th\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <meta http-equiv=\"Cache-Control\" content=\"no-cache\">
  <title>{escaped_name}</title>
  <style>
    body {{ margin: 0; background: #0b1020; color: #e5e7eb; font-family: system-ui, -apple-system, Segoe UI, sans-serif; }}
    main {{ min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; padding: 16px; box-sizing: border-box; }}
    img {{ max-width: min(100%, 1200px); height: auto; background: white; border-radius: 8px; box-shadow: 0 12px 40px rgba(0,0,0,.35); }}
    a {{ color: #93c5fd; word-break: break-all; }}
    .hint {{ font-size: 14px; opacity: .85; text-align: center; }}
  </style>
</head>
<body>
  <main>
    <img src=\"{image_src}\" alt=\"{escaped_name}\" loading=\"eager\" decoding=\"sync\">
    <div class=\"hint\">ถ้าภาพไม่แสดง ให้เปิดไฟล์ PNG โดยตรง: <a href=\"{direct_url}\">{escaped_name}</a></div>
  </main>
</body>
</html>""",
            headers={
                "Cache-Control": "no-store, max-age=0",
                "X-Content-Type-Options": "nosniff",
            },
        )

    try:
        app.mount("/charts", StaticFiles(directory=str(chart_output_dir())), name="line_charts")
        logger.info("Mounted LINE stock chart static directory at /charts")
    except Exception as e:
        logger.warning("Could not mount LINE stock chart static directory: %s", e)
    
    @app.get("/")
    async def root():
        """Health check endpoint."""
        return {
            "status": "running",
            "service": "LINE Webhook Endpoint",
            "version": "1.0.0",
        }
    
    @app.get("/health")
    async def health():
        """Detailed health check."""
        try:
            return {
                "status": "healthy",
                "adapter_configured": _line_adapter is not None,
            }
        except Exception as e:
            return {
                "status": "degraded",
                "error": str(e),
            }
    
    @app.post("/webhook")
    async def webhook(
        request: Request,
        x_line_signature: str = Header(None, alias="X-Line-Signature")
    ):
        """
        LINE webhook endpoint.
        
        Receives messages from LINE and forwards them to Hermes Agent.
        """
        try:
            # Get request body
            body = await request.body()
            body_str = body.decode('utf-8')
            
            logger.warning("📨 Received LINE webhook request")
            logger.debug(f"Signature: {x_line_signature}")
            
            # Validate signature
            if not x_line_signature:
                logger.warning("⚠️ Missing X-Line-Signature header")
                raise HTTPException(status_code=400, detail="Missing signature")
            
            # Check adapter is available
            if not _line_adapter:
                logger.error("❌ LINE adapter not configured")
                raise HTTPException(status_code=503, detail="LINE adapter not available")
            
            # Handle webhook event (this will add event to adapter's pending queue)
            try:
                events = _line_adapter.handle_webhook_event(body_str, x_line_signature)
                logger.warning(f"✅ Parsed {len(events)} LINE event(s)")

                # IMPORTANT: Dispatch parsed events into gateway processing pipeline.
                # Without this, webhook requests return 200 but no reply is ever sent.
                for ev in events:
                    await _line_adapter.handle_message(ev)
                logger.info(f"✅ Dispatched {len(events)} LINE event(s)")
            except Exception as e:
                logger.error(f"❌ Webhook event handling failed: {e}", exc_info=True)
                # Still return 200 for LINE webhook verification (empty events are valid)
                # Only raise HTTP error for actual processing failures
                if "InvalidSignatureError" in str(type(e)):
                    raise HTTPException(status_code=400, detail="Invalid signature")
            
            # Return 200 OK for LINE webhook verification
            return {"status": "ok", "processed": True}
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"❌ Webhook processing error: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=str(e))
    
    return app