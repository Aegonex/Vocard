import aiohttp
import asyncio
import logging
import voicelink

from contextlib import suppress
from discord.ext import commands
from typing import Optional

from .methods import process_methods

class IPCClient:
    def __init__(
        self,
        bot: commands.Bot,
        host: str,
        port: int,
        password: str,
        heartbeat: int = 30,
        secure: bool = False,
        *arg,
        **kwargs
    ) -> None:
        
        self._bot: commands.Bot = bot
        self._host: str = host
        self._port: int = port
        self._password: str = password
        self._heartbeat: int = heartbeat
        self._is_secure: bool = secure
        self._is_connected: bool = False
        self._is_connecting: bool = False
        self._logger: logging.Logger = logging.getLogger("vocard.ipc_client")
        
        self._websocket_url: str = f"{'wss' if self._is_secure else 'ws'}://{self._host}:{self._port}/ws_bot"
        self._session: Optional[aiohttp.ClientSession] = None
        self._websocket: Optional[aiohttp.ClientWebSocketResponse] = None
        self._task: Optional[asyncio.Task] = None

        self._headers = {
            "Authorization": self._password,
            "User-Id": str(bot.user.id),
            "Client-Version": voicelink.Config().version
        }

    async def _listen(self) -> None:
        while True:
            try:
                msg = await self._websocket.receive()
                self._logger.debug(f"Received Message: {msg}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.warning("Dashboard connection closed: %s", exc)
                msg = None

            if msg is None or msg.type in [
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.ERROR,
            ]:
                self._is_connected = False
                self._logger.info("Connection closed. Trying to reconnect in 10s.")
                await asyncio.sleep(10)

                if not self._is_connected:
                    try:
                        await self._open_websocket()
                    except Exception as e:
                        self._logger.error("Reconnection failed: %s", e)
            else:
                self._bot.loop.create_task(process_methods(self, self._bot, msg.json()))

    async def send(self, data: dict):
        # Check if the websocket is still open
        if self.is_connected:
            try:
                await self._websocket.send_json(data)
                self._logger.debug(f"Sent Message: {data}")
            except ConnectionResetError:
                self._logger.warning("Connection lost, attempting to reconnect.")
                await self._handle_reconnect(data)
            except Exception as e:
                self._logger.error(f"Failed to send message: {e}")
        else:
            self._logger.warning("WebSocket is not connected or already closed.")

    async def _handle_reconnect(self, data: dict):
        await self.disconnect()
        await self.connect()
        await asyncio.sleep(1)  # Optional delay before retrying
        if self.is_connected:
            try:
                await self._websocket.send_json(data)
                self._logger.debug(f"Sent Message on reconnect: {data}")
            except Exception as e:
                self._logger.error(f"Failed to send message on reconnect: {e}")
        else:
            self._logger.error("Reconnection failed, not connected.")
                    
    async def _open_websocket(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(
                    total=voicelink.Config().dependency_connect_timeout
                )
            )
        self._websocket = await self._session.ws_connect(
            self._websocket_url, headers=self._headers, heartbeat=self._heartbeat
        )
        self._is_connected = True

    async def connect(self):
        try:
            if self._is_connecting or self._is_connected:
                return
            
            self._is_connecting = True
            await self._open_websocket()

            if self._task is None or self._task.done():
                self._task = self._bot.loop.create_task(self._listen())
            
            self._logger.info("Connected to dashboard!")
        
        except aiohttp.ClientConnectorError as exc:
            self._is_connected = False
            raise ConnectionError("Dashboard connection failed.") from exc
            
        except aiohttp.WSServerHandshakeError as e:
            self._is_connected = False
            raise ConnectionError(
                "Dashboard access forbidden: missing bot ID, version mismatch, or invalid password."
            ) from e
            
        except Exception as e:
            self._is_connected = False
            raise ConnectionError("Could not connect to the dashboard.") from e
        
        finally:
            self._is_connecting = False
            
        return self

    async def disconnect(self) -> None:
        self._is_connected = False
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

        websocket, self._websocket = self._websocket, None
        if websocket is not None and not websocket.closed:
            with suppress(Exception):
                await websocket.close()

        session, self._session = self._session, None
        if session is not None and not session.closed:
            await session.close()

        self._logger.info("Disconnected to dashboard!")
    
    @property
    def is_connected(self) -> bool:
        return bool(
            self._is_connected
            and self._websocket is not None
            and not self._websocket.closed
        )
