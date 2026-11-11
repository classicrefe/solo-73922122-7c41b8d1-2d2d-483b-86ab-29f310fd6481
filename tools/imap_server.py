"""最小 IMAP4rev1 服务器（仅依赖 Python 标准库）。

用途：为采集功能提供开箱即用的本地邮件源，无需任何外部邮箱账号。

支持的命令（覆盖标准库 imaplib 的采集路径）：

    CAPABILITY / NOOP / LOGOUT
    LOGIN <user> <password>
    SELECT <mailbox>            返回 UIDVALIDITY、EXISTS、FLAGS
    UID SEARCH ALL              返回全部邮件 UID（递增）
    UID FETCH <uids> (RFC822)   按 UID 返回整封邮件

断线演示
========
设置环境变量 ``IMAPD_DROP_UID=<uid>`` 后，每个连接在 FETCH 到该 UID、
发送部分负载后直接断开 TCP，模拟采集途中掉线；本进程只触发一次，
重启容器（或清空该变量）即可恢复，便于演示“停在该封之前、重启续采”。

邮件来源
========
``--mail-dir`` 指向的目录中每个 ``.eml`` 文件是一封完整 RFC822 邮件，
按文件名排序后从 ``--uid-start``（默认 1）起依次分配 UID。
"""

from __future__ import annotations

import argparse
import logging
import os
import socketserver
import threading
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("lab.imapd")

CAPABILITIES = (
    b"IMAP4rev1 UIDPLUS UNSELECT "
    b"AUTH=PLAIN LOGINDISABLED"
)
# 上面 LOGINDISABLED 会让部分客户端认为不能登录，演示源允许明文 LOGIN：
CAPABILITIES = b"IMAP4rev1 UIDPLUS UNSELECT"


class MessageStore:
    """内存邮件库。

    生产形态在启动时一次性加载；演示场景下允许通过
    ``reload_if_changed`` 在检测到目录内容变化时热重载，
    这样修复坏邮件后不必重启演示进程（也避免脚本依赖 kill）。
    """

    def __init__(
        self,
        mail_dir: str,
        uidvalidity: int,
        uid_start: int = 1,
        drop_uid: Optional[int] = None,
        auto_reload: bool = True,
    ) -> None:
        self.mail_dir = mail_dir
        self.uidvalidity = int(uidvalidity)
        self.uid_start = int(uid_start)
        self.drop_uid = drop_uid
        self.auto_reload = auto_reload
        self._lock = threading.Lock()
        self._drop_armed = drop_uid is not None
        self.messages: Dict[int, bytes] = {}
        self._fingerprint: tuple = ()
        self._load()

    def _fingerprint_of_dir(self) -> tuple:
        """目录指纹：文件名 + 大小 + 修改时间，变化即触发重载。"""
        if not os.path.isdir(self.mail_dir):
            return ()
        entries = []
        for name in sorted(os.listdir(self.mail_dir)):
            if not name.lower().endswith(".eml"):
                continue
            stat = os.stat(os.path.join(self.mail_dir, name))
            entries.append((name, stat.st_size, stat.st_mtime_ns))
        return tuple(entries)

    def _load(self) -> None:
        if not os.path.isdir(self.mail_dir):
            raise FileNotFoundError(f"邮件目录不存在: {self.mail_dir}")
        names = sorted(
            name for name in os.listdir(self.mail_dir)
            if name.lower().endswith(".eml")
        )
        loaded: Dict[int, bytes] = {}
        for offset, name in enumerate(names):
            uid = self.uid_start + offset
            with open(os.path.join(self.mail_dir, name), "rb") as handle:
                loaded[uid] = handle.read()
        self.messages = loaded
        self._fingerprint = self._fingerprint_of_dir()
        logger.info("已加载 %s 封邮件，UID 范围 %s..%s",
                    len(loaded),
                    self.uid_start,
                    self.uid_start + len(names) - 1)

    def reload_if_changed(self) -> bool:
        """目录变化时原子热重载；返回是否发生了重载。"""
        if not self.auto_reload:
            return False
        fingerprint = self._fingerprint_of_dir()
        if fingerprint == self._fingerprint:
            return False
        with self._lock:
            fingerprint = self._fingerprint_of_dir()
            if fingerprint == self._fingerprint:
                return False
            logger.info("检测到邮件目录变化，热重载邮件库")
            self._load()
            return True

    def all_uids(self) -> List[int]:
        self.reload_if_changed()
        with self._lock:
            return sorted(self.messages)

    def get(self, uid: int) -> Optional[bytes]:
        self.reload_if_changed()
        with self._lock:
            return self.messages.get(uid)

    def should_drop(self, uid: int) -> bool:
        """断线演示：全局只触发一次，避免反复切断所有连接。"""
        if self.drop_uid is None or uid != self.drop_uid:
            return False
        with self._lock:
            if self._drop_armed:
                self._drop_armed = False
                return True
            return False

    def arm_drop(self) -> None:
        """重新武装断线开关（模拟下一次连接再次发生断线）。"""
        with self._lock:
            self._drop_armed = True

    def clear_drop(self) -> None:
        """关闭断线演示，让后续连接恢复正常。"""
        with self._lock:
            self._drop_armed = False
            self.drop_uid = None


class _Connection:
    """单条 TCP 连接的会话状态与命令处理。"""

    def __init__(self, sock, addr, store: MessageStore,
                 username: str, password: str, mailbox: str) -> None:
        self.sock = sock
        self.addr = addr
        self.store = store
        self.username = username
        self.password = password
        self.mailbox = mailbox
        self.authenticated = False
        self.selected = False
        self._recv_buf = b""

    # ------------------------------------------------------------------ #
    # 字节流读取：支持行输入与 {n} 字面量
    # ------------------------------------------------------------------ #

    def _read_line(self) -> bytes:
        while b"\r\n" not in self._recv_buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("客户端断开")
            self._recv_buf += chunk
        line, self._recv_buf = self._recv_buf.split(b"\r\n", 1)
        return line

    def read_command(self) -> bytes:
        """读取一条完整命令（含可能的字面量参数）。"""
        parts: List[bytes] = []
        line = self._read_line()
        while b"{" in line:
            head, literal_tag = line.rsplit(b"{", 1)
            size_text = literal_tag.rstrip(b"}").rstrip(b"+")
            try:
                size = int(size_text)
            except ValueError:
                break
            literal = self._read_exact(size)
            # 消费字面量后的 CRLF
            tail = self._read_line()
            parts.append(head)
            parts.append(literal)
            line = tail
        parts.append(line)
        return b" ".join(p for p in parts if p)

    def _read_exact(self, size: int) -> bytes:
        while len(self._recv_buf) < size:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("读取字面量时客户端断开")
            self._recv_buf += chunk
        data = self._recv_buf[:size]
        self._recv_buf = self._recv_buf[size:]
        return data

    def send(self, data: bytes) -> None:
        if not data.endswith(b"\r\n"):
            data += b"\r\n"
        self.sock.sendall(data)

    def send_tagged_ok(self, tag: bytes, text: bytes = b"OK") -> None:
        self.send(tag + b" OK " + text)

    # ------------------------------------------------------------------ #
    # 主循环
    # ------------------------------------------------------------------ #

    def serve(self) -> None:
        self.send(b"* OK [CAPABILITY " + CAPABILITIES +
                  b"] local IMAP demo server ready")
        while True:
            try:
                command = self.read_command()
            except (ConnectionError, OSError):
                logger.info("连接 %s 断开", self.addr)
                return
            if not command.strip():
                continue
            try:
                done = self.dispatch(command)
            except ConnectionError:
                logger.warning("连接 %s 被对端重置", self.addr)
                return
            if done:
                return

    def dispatch(self, command: bytes) -> bool:
        """处理一条命令，返回 True 表示结束连接。"""
        tokens = command.split()
        if len(tokens) < 2:
            self.send("* BAD 无法解析的命令".encode())
            return False
        tag, verb = tokens[0], tokens[1].upper()
        args = tokens[2:]

        if verb == b"CAPABILITY":
            self.send(b"* CAPABILITY " + CAPABILITIES)
            self.send_tagged_ok(tag, b"CAPABILITY completed")
        elif verb == b"NOOP":
            self.send_tagged_ok(tag, b"NOOP completed")
        elif verb == b"LOGOUT":
            self.send(b"* BYE logging out")
            self.send_tagged_ok(tag, b"LOGOUT completed")
            return True
        elif verb == b"LOGIN":
            self._handle_login(tag, args)
        elif verb == b"SELECT" or verb == b"EXAMINE":
            self._handle_select(tag, args)
        elif verb == b"UID":
            self._handle_uid(tag, args)
        elif verb in (b"STARTTLS", b"AUTHENTICATE"):
            self.send(tag + " BAD 演示源不支持该命令，请使用明文 LOGIN".encode())
        else:
            self.send(tag + b" BAD unsupported command: " + verb)
        return False

    # ------------------------------------------------------------------ #
    # 命令实现
    # ------------------------------------------------------------------ #

    def _handle_login(self, tag: bytes, args: List[bytes]) -> None:
        if len(args) < 2:
            self.send(tag + " BAD LOGIN 需要用户名和密码".encode())
            return
        user = _unquote(args[0])
        password = _unquote(args[1])
        if user != self.username or password != self.password:
            self.send(tag + " NO [AUTHENTICATIONFAILED] 凭证错误".encode())
            return
        self.authenticated = True
        self.send(b"* CAPABILITY " + CAPABILITIES)
        self.send_tagged_ok(tag, b"LOGIN completed")

    def _handle_select(self, tag: bytes, args: List[bytes]) -> None:
        if not self.authenticated:
            self.send(tag + " BAD 请先 LOGIN".encode())
            return
        mailbox = _unquote(args[0]) if args else self.mailbox
        if mailbox != self.mailbox:
            self.send(tag + " NO 邮箱不存在: ".encode() + mailbox.encode())
            return
        self.selected = True
        count = len(self.store.messages)
        self.send(f"* {count} EXISTS".encode())
        self.send(b"* 0 RECENT")
        self.send(b"* FLAGS (\\Seen \\Answered \\Flagged \\Deleted \\Draft)")
        self.send(
            f"* OK [UIDVALIDITY {self.store.uidvalidity}] uidvalidity".encode()
        )
        self.send(b"* OK [UIDNEXT " +
                  str(max(self.store.all_uids(), default=0) + 1).encode() +
                  b"] next uid")
        self.send_tagged_ok(tag, b"[READ-WRITE] SELECT completed")

    def _handle_uid(self, tag: bytes, args: List[bytes]) -> None:
        if not self.authenticated or not self.selected:
            self.send(tag + " BAD 请先 LOGIN 并 SELECT".encode())
            return
        if not args:
            self.send(tag + " BAD UID 需要子命令".encode())
            return
        sub = args[0].upper()
        if sub == b"SEARCH":
            self._handle_uid_search(tag, args[1:])
        elif sub == b"FETCH":
            self._handle_uid_fetch(tag, args[1:])
        else:
            self.send(tag + " BAD 仅支持 UID SEARCH / UID FETCH".encode())

    def _handle_uid_search(self, tag: bytes, args: List[bytes]) -> None:
        # 演示源只有全部可见邮件，SEARCH 条件（ALL 等）统一返回全集。
        uids = self.store.all_uids()
        payload = b"* SEARCH " + " ".join(str(uid) for uid in uids).encode()
        self.send(payload)
        self.send_tagged_ok(tag, b"UID SEARCH completed")

    def _handle_uid_fetch(self, tag: bytes, args: List[bytes]) -> None:
        if not args:
            self.send(tag + " BAD FETCH 需要 UID 集合".encode())
            return
        uids = _parse_uid_set(args[0], self.store)
        # IMAP 未标记响应的序号是消息序列号；本演示源 UID 连续且从
        # uid_start 起，序列号 = uid - uid_start + 1。
        for uid in uids:
            message = self.store.get(uid)
            if message is None:
                # 不存在的 UID 静默跳过，符合 IMAP 语义。
                continue
            seq = uid - self.store.uid_start + 1

            if self.store.should_drop(uid):
                # 模拟传输中断：发出 FETCH 头与部分负载后直接关连接，
                # 不发送任何 tagged 响应。
                logger.warning("断线演示：UID %s 发送中途断开 TCP", uid)
                partial = message[: max(1, len(message) // 2)]
                self.sock.sendall(
                    f"* {seq} FETCH (FLAGS (\\Seen) UID {uid} "
                    f"RFC822 {{{len(partial)}}}\r\n".encode() + partial
                )
                try:
                    self.sock.shutdown(__import__("socket").SHUT_RDWR)
                except OSError:
                    pass
                self.sock.close()
                raise ConnectionError("演示断线")

            # 标准帧：字面量必须是括号组的最后一项，整封邮件作为
            # 单个字面量发送，帧尾的 ')' 与字面量数据同处一条响应。
            self.sock.sendall(
                f"* {seq} FETCH (FLAGS (\\Seen) UID {uid} "
                f"RFC822 {{{len(message)}}}\r\n".encode()
                + message
                + b")\r\n"
            )
        self.send_tagged_ok(tag, b"UID FETCH completed")


def _unquote(token: bytes) -> str:
    if len(token) >= 2 and token[0:1] == b'"' and token[-1:] == b'"':
        return token[1:-1].replace(b'\\"', b'"').decode("utf-8", "replace")
    return token.decode("utf-8", "replace")


def _parse_uid_set(token: bytes, store: MessageStore) -> List[int]:
    """解析 ``1,3,5:7`` 形式的 UID 集合，仅返回存在的 UID，保持递增。"""
    uids: List[int] = []
    known = set(store.all_uids())
    for part in token.split(b","):
        if b":" in part:
            start_text, end_text = part.split(b":", 1)
            start = int(start_text)
            end = int(end_text)
            lo, hi = sorted((start, end))
            uids.extend(uid for uid in range(lo, hi + 1) if uid in known)
        else:
            uid = int(part)
            if uid in known:
                uids.append(uid)
    return sorted(set(uids))


class ThreadedImapServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, server_address: Tuple[str, int], handler_factory,
                 store: MessageStore) -> None:
        self.store = store
        self.handler_factory = handler_factory
        super().__init__(server_address, _TcpHandler)


def _TcpHandler(request, client_address, server: ThreadedImapServer) -> None:  # noqa: N802
    connection = server.handler_factory(request, client_address, server.store)
    try:
        connection.serve()
    except Exception:
        logger.exception("连接处理异常: %s", client_address)
    finally:
        try:
            request.close()
        except OSError:
            pass


def build_server(host: str, port: int, store: MessageStore,
                 username: str, password: str, mailbox: str) -> ThreadedImapServer:
    def factory(sock, addr, message_store: MessageStore) -> _Connection:
        return _Connection(sock, addr, message_store, username, password, mailbox)

    return ThreadedImapServer((host, port), factory, store)


def _optional_int(raw: str | None) -> int | None:
    """argparse 类型：空字符串/空白视为未设置（None）。"""
    if raw is None or not raw.strip():
        return None
    return int(raw)


def main() -> None:
    parser = argparse.ArgumentParser(description="本地 IMAP 演示服务器")
    parser.add_argument("--host", default=os.environ.get("IMAPD_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("IMAPD_PORT", "3143")))
    parser.add_argument("--mail-dir",
                        default=os.environ.get("IMAPD_MAIL_DIR", "/mailbox"))
    parser.add_argument("--uidvalidity", type=int,
                        default=int(os.environ.get("IMAPD_UIDVALIDITY", "1700000000")))
    parser.add_argument("--uid-start", type=int,
                        default=int(os.environ.get("IMAPD_UID_START", "1")))
    parser.add_argument("--user", default=os.environ.get("IMAPD_USER", "lab"))
    parser.add_argument("--password",
                        default=os.environ.get("IMAPD_PASSWORD", "labpass"))
    parser.add_argument("--mailbox",
                        default=os.environ.get("IMAPD_MAILBOX", "INBOX"))
    parser.add_argument("--drop-uid", type=_optional_int,
                        default=_optional_int(os.environ.get("IMAPD_DROP_UID")))
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    store = MessageStore(
        mail_dir=args.mail_dir,
        uidvalidity=args.uidvalidity,
        uid_start=args.uid_start,
        drop_uid=args.drop_uid,
    )
    server = build_server(
        args.host, args.port, store,
        args.user, args.password, args.mailbox,
    )
    logger.info(
        "IMAP 演示服务器监听 %s:%s (mailbox=%s, uidvalidity=%s, drop_uid=%s)",
        args.host, args.port, args.mailbox, args.uidvalidity, args.drop_uid,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()
