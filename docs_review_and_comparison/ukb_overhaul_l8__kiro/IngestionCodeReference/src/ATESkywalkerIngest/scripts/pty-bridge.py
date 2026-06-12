#!/usr/bin/env python3
"""
PTY bridge for driving an interactive TTY program (TST live run) from a
non-interactive agent.

- Launches the target command in a pseudo-terminal so the program believes it
  has a real TTY (required by `tst ... --interactive`).
- Continuously appends all PTY output to LOG so it can be tailed/read.
- Reads keystrokes from a FIFO (CMD) and forwards them to the PTY stdin, so the
  agent can send 'h', 'l', 'r', 'Y\\n', etc., by writing to the FIFO.

Usage:
  python3 pty-bridge.py <log_path> <fifo_path> -- <command...>
"""
import os
import pty
import select
import sys
import errno


def main():
    sep = sys.argv.index("--")
    log_path = sys.argv[1]
    fifo_path = sys.argv[2]
    cmd = sys.argv[sep + 1:]

    if not os.path.exists(fifo_path):
        os.mkfifo(fifo_path)

    log = open(log_path, "ab", buffering=0)

    pid, master_fd = pty.fork()
    if pid == 0:
        # Child: exec the target command with a sane terminal type.
        os.environ.setdefault("TERM", "xterm-256color")
        os.execvp(cmd[0], cmd)
        os._exit(127)

    # Parent: open the FIFO for reading (non-blocking) plus a write handle so
    # the open() never blocks waiting for a writer and EOF is not signalled when
    # a transient writer closes.
    fifo_r = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
    fifo_w = os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK)

    try:
        while True:
            rlist, _, _ = select.select([master_fd, fifo_r], [], [], 1.0)
            if master_fd in rlist:
                try:
                    data = os.read(master_fd, 4096)
                except OSError as e:
                    if e.errno == errno.EIO:
                        break  # child exited
                    raise
                if not data:
                    break
                log.write(data)
            if fifo_r in rlist:
                try:
                    cmd_data = os.read(fifo_r, 4096)
                except OSError:
                    cmd_data = b""
                if cmd_data:
                    os.write(master_fd, cmd_data)
            # Reap child if it has exited and the pty has drained.
            try:
                wpid, _ = os.waitpid(pid, os.WNOHANG)
                if wpid == pid:
                    # Drain any remaining output.
                    try:
                        while True:
                            data = os.read(master_fd, 4096)
                            if not data:
                                break
                            log.write(data)
                    except OSError:
                        pass
                    break
            except ChildProcessError:
                break
    finally:
        for fd in (fifo_r, fifo_w, master_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        log.close()


if __name__ == "__main__":
    main()
