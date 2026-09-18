"""
Маленький SOCKS5-сервер для домашней стороны туннеля.

Зачем: бот на сервере ходит в интернет через наш домашний адрес. Раньше эту роль играл
обратный SSH-туннель — ssh умеет быть SOCKS-прокси сам. Но он заворачивает TCP внутрь
TCP и гонит всё через ОДНО соединение: параллельные запросы вставали в очередь (замер с
сервера: 10 мелких запросов разом занимали 3.2с вместо 0.4с), плюс +0.19с накладных на
каждый запрос. yt-dlp делает десятки запросов на один ролик — отсюда и десятки секунд.

Теперь транспорт — WireGuard (UDP, в ядре, без очереди), а SOCKS-частью работает этот
скрипт. Он слушает ТОЛЬКО адрес внутри туннеля, так что из интернета недоступен: чтобы
до него достучаться, нужен ключ WireGuard.

Запуск:
    python tools/socks_server.py
    python tools/socks_server.py --host 10.8.0.2 --port 1080

Проверить с сервера (должен ответить ДОМАШНИЙ адрес):
    curl --proxy socks5h://10.8.0.2:1080 https://api.ipify.org
"""
import argparse
import asyncio
import ipaddress
import logging
import socket
import struct

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("socks")

# Размер куска при перекачке. 64 КБ — компромисс: меньше даёт лишние системные вызовы
# на больших файлах, больше почти не ускоряет, но съедает память на каждое соединение.
CHUNK = 65536
# Сколько ждать соединения с сайтом. yt-dlp сам повторяет неудачные запросы, так что
# лучше быстро отдать отказ, чем держать зависшее соединение.
CONNECT_TIMEOUT = 20


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Перекачивает данные в одну сторону, пока не кончатся."""
    try:
        while True:
            data = await reader.read(CHUNK)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        # Закрываем только свою половину: встречный поток может ещё дописывать.
        try:
            writer.close()
        except OSError:
            pass


async def _read_target(reader: asyncio.StreamReader) -> tuple[str, int]:
    """Разбирает запрос SOCKS5 и возвращает, куда клиент хочет подключиться."""
    ver, nmethods = struct.unpack("!BB", await reader.readexactly(2))
    if ver != 5:
        raise ValueError(f"не SOCKS5 (версия {ver})")
    await reader.readexactly(nmethods)          # список способов входа — нам не нужен

    ver, cmd, _, atyp = struct.unpack("!BBBB", await reader.readexactly(4))
    if cmd != 1:
        raise ValueError(f"поддерживаем только CONNECT (пришло {cmd})")

    if atyp == 1:                               # IPv4
        host = socket.inet_ntoa(await reader.readexactly(4))
    elif atyp == 3:                             # доменное имя
        ln = (await reader.readexactly(1))[0]
        host = (await reader.readexactly(ln)).decode()
    elif atyp == 4:                             # IPv6
        host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
    else:
        raise ValueError(f"неизвестный тип адреса {atyp}")

    port = struct.unpack("!H", await reader.readexactly(2))[0]
    return host, port


def _reply(code: int, host: str = "0.0.0.0", port: int = 0) -> bytes:
    """Ответ клиенту: 0 — успех, остальное — отказ."""
    try:
        addr = ipaddress.ip_address(host)
        if addr.version == 4:
            return b"\x05" + bytes([code]) + b"\x00\x01" + addr.packed + struct.pack("!H", port)
        return b"\x05" + bytes([code]) + b"\x00\x04" + addr.packed + struct.pack("!H", port)
    except ValueError:
        return b"\x05" + bytes([code]) + b"\x00\x01" + b"\x00\x00\x00\x00" + struct.pack("!H", port)


async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    peer = client_writer.get_extra_info("peername")
    remote_writer = None
    try:
        client_writer.write(b"\x05\x00")        # SOCKS5, вход без пароля
        await client_writer.drain()
        host, port = await _read_target(client_reader)

        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), CONNECT_TIMEOUT)
        except Exception as e:
            logger.info("не подключился к %s:%s — %s", host, port, type(e).__name__)
            client_writer.write(_reply(5))      # «отказано»
            await client_writer.drain()
            return

        # Отдаём клиенту адрес, с которого реально вышли: yt-dlp этим не пользуется,
        # но по стандарту поле обязано быть заполнено.
        local = remote_writer.get_extra_info("sockname") or ("0.0.0.0", 0)
        client_writer.write(_reply(0, local[0], local[1]))
        await client_writer.drain()

        await asyncio.gather(_pipe(client_reader, remote_writer),
                             _pipe(remote_reader, client_writer))
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        pass                                     # клиент отвалился — обычное дело
    except ValueError as e:
        logger.info("кривой запрос от %s: %s", peer, e)
    finally:
        for w in (client_writer, remote_writer):
            if w is not None:
                try:
                    w.close()
                except OSError:
                    pass


async def main() -> None:
    ap = argparse.ArgumentParser(description="SOCKS5 для домашней стороны туннеля")
    # По умолчанию слушаем ТОЛЬКО адрес внутри WireGuard. Не 0.0.0.0: открытый SOCKS
    # в локальной сети (а тем более в интернете) — это чужой трафик под нашим адресом.
    ap.add_argument("--host", default="10.8.0.2")
    ap.add_argument("--port", type=int, default=1080)
    args = ap.parse_args()

    server = await asyncio.start_server(handle, args.host, args.port)
    logger.info("SOCKS5 слушает %s:%s", args.host, args.port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
