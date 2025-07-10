import asyncio
import gzip
import io
import logging
import time
from pathlib import Path

import make87 as m87
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from starlette.middleware.gzip import GZipMiddleware
from uvicorn import Config, Server

from make87.interfaces.zenoh import ZenohInterface
from make87_messages.image.compressed.image_jpeg_pb2 import ImageJPEG
from make87_messages.text.text_plain_pb2 import PlainText

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

connected_clients = set()


async def serve_ui_with_zenoh():
    config = m87.config.load_config_from_env()
    app = FastAPI()
    app.add_middleware(GZipMiddleware, minimum_size=500)

    zenoh_interface = ZenohInterface(name="zenoh-client", make87_config=config)

    @app.websocket("/ws-agent")
    async def ws_chat(ws: WebSocket):
        await ws.accept()
        try:
            requester = zenoh_interface.get_requester(name="agent-chat")
            while True:
                msg = await ws.receive_text()
                try:
                    response = requester.get(payload=msg.encode("utf-8"))
                    for r in response:
                        if r.ok is not None:
                            response = r.ok.payload.to_bytes().decode("utf-8")
                            await ws.send_text(response)
                except Exception as e:
                    await ws.send_text(f"Error processing message: {e}")
        except WebSocketDisconnect:
            logging.info("WebSocket disconnected")
        except KeyError as e:
            await ws.send_text("No agent available to chat with.")

    @app.websocket("/ws-image")
    async def websocket_image(ws: WebSocket):
        await ws.accept()
        connected_clients.add(ws)
        logging.info("New WebSocket image client connected.")
        await ws.send_bytes(b'\xff\xd8\xff\xd9')  # Dummy JPEG marker

        try:
            image_sub = zenoh_interface.get_subscriber(name="CAMERA_IMAGE")
            while True:
                sample = image_sub.try_recv()
                if not sample:
                    await asyncio.sleep(0.1)
                    continue
                image = m87.encodings.ProtobufEncoder(message_type=ImageJPEG).decode(sample.payload.to_bytes())
                await ws.send_bytes(image.data)
        except WebSocketDisconnect:
            logging.info("WebSocket image client disconnected.")
        except KeyError as e:
            pass
        finally:
            connected_clients.discard(ws)

    @app.websocket("/ws-status")
    async def agent_status_stream(ws: WebSocket):
        await ws.accept()
        await ws.send_text("connected")

        try:
            log_sub = zenoh_interface.get_subscriber(name="AGENT_LOGS")
            while True:
                sample = log_sub.try_recv()
                if sample:
                    utf8encoded = sample.payload.to_bytes()
                    log = utf8encoded.decode("utf-8")
                    await ws.send_text(log)
                else:
                    await asyncio.sleep(0.1)
        except WebSocketDisconnect:
            logging.info("WebSocket log client disconnected.")
        except KeyError as e:
            await ws.send_text("no logs available")


    @app.get("/")
    @app.get("/{path:path}")
    async def serve_chat_ui(request: Request, path: str = ""):
        html_path = Path(__file__).parent / "static" / "chat.html"
        with html_path.open("rb") as f:
            content = f.read()
        compressed = io.BytesIO()
        with gzip.GzipFile(fileobj=compressed, mode="wb") as gz:
            gz.write(content)
        compressed.seek(0)
        return StreamingResponse(
            compressed,
            media_type="text/html",
            headers={
                "Content-Encoding": "gzip",
                "Connection": "close",
            },
        )

    config = Config(app=app, host="0.0.0.0", port=8089, loop="asyncio", timeout_keep_alive=0, log_config=None)
    await Server(config).serve()



logging.basicConfig(level=logging.INFO)



if __name__ == "__main__":
    asyncio.run(serve_ui_with_zenoh())