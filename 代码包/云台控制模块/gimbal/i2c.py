"""无依赖 I2C 访问（Linux /dev/i2c-N）。

不依赖 smbus2：Bianbu 上 smbus2 未必装好，而 python3-smbus 又常有版本冲突。
这里直接用 ioctl(I2C_SLAVE) + os.read/os.write，等价于一次 SMBus 传输。

硬件前提：
    sudo usermod -aG i2c $USER    # 之后重新登录
    # 或临时放权：sudo chmod a+rw /dev/i2c-4
"""

import os
import threading

try:
    import fcntl
except ImportError:          # 在 Windows 上只做 selftest 时需要能 import
    fcntl = None

I2C_SLAVE = 0x0703
I2C_SLAVE_FORCE = 0x0706


class I2CError(OSError):
    pass


class I2CBus:
    """一条总线上挂多个器件时，select(addr) 与读写必须成对原子执行。

    本工程里 PCA9685(0x40) 由 100 Hz 控制线程写、MPU6050(0x68) 由主线程读，
    两者共用同一条 /dev/i2c-4。若不加锁，会出现"选好 0x68 却被另一线程
    改成 0x40"的串扰，表现为偶尔读到 0 或 MPU 数据跳变。
    """

    def __init__(self, bus_num, force=False):
        if fcntl is None:
            raise I2CError("当前环境没有 fcntl，I2C 只能在 Linux 板上使用")
        self.bus_num = bus_num
        self.path = "/dev/i2c-%d" % bus_num
        self._fd = os.open(self.path, os.O_RDWR)
        self._force = force
        self._sel = None
        self._lock = threading.RLock()

    # ---------- 裸操作（调用方必须已持锁） ----------
    def _select(self, addr):
        if self._sel == addr:
            return
        code = I2C_SLAVE_FORCE if self._force else I2C_SLAVE
        try:
            fcntl.ioctl(self._fd, code, addr)
        except OSError as e:
            raise I2CError("无法选中 0x%02X（bus=%d）：%s" % (addr, self.bus_num, e))
        self._sel = addr

    def _w(self, addr, data):
        self._select(addr)
        os.write(self._fd, bytes(data))

    def _r(self, addr, n):
        self._select(addr)
        return os.read(self._fd, n)

    # ---------- 对外接口（自带锁） ----------
    def write(self, addr, data):
        with self._lock:
            self._w(addr, data)

    def read(self, addr, n):
        with self._lock:
            return self._r(addr, n)

    def write_reg(self, addr, reg, val):
        with self._lock:
            self._w(addr, [reg & 0xFF, val & 0xFF])

    def write_regs(self, addr, reg, data):
        with self._lock:
            self._w(addr, [reg & 0xFF] + list(data))

    def read_reg(self, addr, reg):
        with self._lock:
            self._w(addr, [reg & 0xFF])
            r = self._r(addr, 1)
        if len(r) != 1:
            raise I2CError("读 0x%02X[0x%02X] 返回 %d 字节" % (addr, reg, len(r)))
        return r[0]

    def read_regs(self, addr, reg, n):
        with self._lock:
            self._w(addr, [reg & 0xFF])
            r = self._r(addr, n)
        if len(r) != n:
            raise I2CError("读 0x%02X[0x%02X] 期望 %d 字节，实得 %d"
                           % (addr, reg, n, len(r)))
        return r

    def close(self):
        if self._fd is not None:
            try:
                os.close(self._fd)
            finally:
                self._fd = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def scan(bus_num, addrs=None):
    """扫描总线上应答的地址，返回列表。不抛异常。"""
    addrs = addrs if addrs is not None else range(0x03, 0x78)
    found = []
    try:
        bus = I2CBus(bus_num)
    except (OSError, I2CError):
        return found
    try:
        for a in addrs:
            try:
                bus._select(a)
                os.read(bus._fd, 1)
            except (OSError, I2CError):
                continue
            found.append(a)
    finally:
        bus.close()
    return found


def list_buses():
    """返回系统上所有 /dev/i2c-N 的编号。"""
    out = []
    try:
        for name in os.listdir("/dev"):
            if name.startswith("i2c-"):
                try:
                    out.append(int(name[4:]))
                except ValueError:
                    pass
    except OSError:
        pass
    return sorted(out)


def detect_bus(candidates, want_addr):
    """在候选总线里找挂着 want_addr 的那条，返回总线号或 None。"""
    for b in candidates:
        if b in list_buses() and want_addr in scan(b, [want_addr]):
            return b
    for b in list_buses():
        if want_addr in scan(b, [want_addr]):
            return b
    return None
