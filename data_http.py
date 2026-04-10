"""
data_http.py - HTTP 请求管理层

提供 Session 复用、UA 轮换、请求头管理等基础能力，
让数据获取请求更像真实浏览器访问，降低被反爬虫识别的概率。

Features:
- Session 复用（保持 TCP 连接池）
- User-Agent 轮换（模拟不同浏览器）
- 完整请求头（Accept、Accept-Language 等）
- 线程安全
- Session 刷新机制（错误恢复）
"""

import requests
import random
import time
import threading
import logging
from typing import Optional

logger = logging.getLogger("grid_trading")

# 常用浏览器 UA 列表（当 fake_useragent 不可用时备用）
FALLBACK_USER_AGENTS = [
    # Chrome on Windows
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
    # Chrome on macOS
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
    # Firefox on Windows
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0',
    # Firefox on macOS
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0',
    # Safari on macOS
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
    # Edge on Windows
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0',
    # Chrome on Linux
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
]

# 完整请求头模板
DEFAULT_HEADERS = {
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Accept-Encoding': 'gzip, deflate, br',
    'DNT': '1',
    'Upgrade-Insecure-Requests': '1',
}


class HTTPSessionManager:
    """
    HTTP 会话管理器

    功能:
    - Session 复用（保持 TCP 连接池，避免频繁建立连接）
    - User-Agent 轮换（模拟不同浏览器，降低被识别概率）
    - 完整请求头（让请求更像真实浏览器）
    - 线程安全
    - Session 刷新机制（出错后可刷新）

    Usage:
        from data_http import get_session_manager

        mgr = get_session_manager()
        session = mgr.get_session()
        response = session.get(url)
    """

    def __init__(
        self,
        ua_change_interval: int = 30,
        pool_connections: int = 10,
        pool_maxsize: int = 20,
    ):
        """
        初始化 HTTP 会话管理器

        Parameters:
            ua_change_interval: UA 更换间隔（秒），默认 30 秒
            pool_connections: 连接池连接数，默认 10
            pool_maxsize: 连接池最大连接数，默认 20
        """
        self._ua_change_interval = ua_change_interval
        self._pool_connections = pool_connections
        self._pool_maxsize = pool_maxsize

        self._session: Optional[requests.Session] = None
        self._lock = threading.Lock()
        self._ua_generator = None
        self._last_ua_change = 0
        self._consecutive_failures = 0

    def _init_ua_generator(self):
        """延迟初始化 UA 生成器"""
        if self._ua_generator is None:
            try:
                from fake_useragent import UserAgent
                self._ua_generator = UserAgent()
                logger.debug("fake_useragent 初始化成功")
            except ImportError:
                logger.warning("fake_useragent 未安装，将使用备用 UA 列表")
            except Exception as e:
                logger.warning(f"fake_useragent 初始化失败: {e}，将使用备用 UA 列表")

    def get_random_ua(self) -> str:
        """
        获取随机 User-Agent

        Returns:
            随机选择的 User-Agent 字符串
        """
        self._init_ua_generator()

        if self._ua_generator is not None:
            try:
                ua = self._ua_generator.random
                if ua and len(ua) > 10:  # 有效性检查
                    return ua
            except Exception:
                pass

        # 备用方案：从固定列表随机选择
        return random.choice(FALLBACK_USER_AGENTS)

    def get_session(self) -> requests.Session:
        """
        获取或创建 Session（线程安全）

        Returns:
            复用或新创建的 requests.Session 对象
        """
        with self._lock:
            if self._session is None:
                self._session = requests.Session()

                # 设置基础请求头
                self._session.headers.update(DEFAULT_HEADERS)
                self._session.headers['User-Agent'] = self.get_random_ua()

                # 设置连接适配器（实现连接池）
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=self._pool_connections,
                    pool_maxsize=self._pool_maxsize,
                    max_retries=0,  # 重试由调用方控制
                    pool_block=False,
                )
                self._session.mount('http://', adapter)
                self._session.mount('https://', adapter)

                logger.info(
                    f"HTTP Session 创建成功: "
                    f"pool={self._pool_connections}/{self._pool_maxsize}, "
                    f"UA={self._session.headers['User-Agent'][:50]}..."
                )

            # 定期更换 UA
            current_time = time.time()
            if current_time - self._last_ua_change > self._ua_change_interval:
                old_ua = self._session.headers.get('User-Agent', 'N/A')[:30]
                new_ua = self.get_random_ua()
                self._session.headers['User-Agent'] = new_ua
                self._last_ua_change = current_time
                logger.debug(f"UA 更新: {old_ua}... -> {new_ua}...")

            return self._session

    def refresh_session(self):
        """
        强制刷新 Session

        在连续失败后调用，创建新的连接池，放弃有问题的旧连接
        """
        with self._lock:
            if self._session is not None:
                try:
                    self._session.close()
                    logger.info("旧 Session 已关闭")
                except Exception as e:
                    logger.warning(f"关闭 Session 时出错: {e}")
                self._session = None
                self._consecutive_failures = 0

    def record_success(self):
        """记录一次成功请求，连续失败计数清零"""
        with self._lock:
            self._consecutive_failures = 0

    def record_failure(self) -> bool:
        """
        记录一次失败请求

        Returns:
            是否应该刷新 Session（连续失败达到阈值时返回 True）
        """
        with self._lock:
            self._consecutive_failures += 1
            should_refresh = self._consecutive_failures >= 3
            if should_refresh:
                logger.warning(
                    f"连续失败 {self._consecutive_failures} 次，"
                    f"建议刷新 Session"
                )
            return should_refresh

    def get_status(self) -> dict:
        """
        获取当前状态

        Returns:
            包含状态信息的字典
        """
        with self._lock:
            return {
                'has_session': self._session is not None,
                'consecutive_failures': self._consecutive_failures,
                'last_ua_change_seconds_ago': (
                    time.time() - self._last_ua_change
                    if self._last_ua_change > 0 else None
                ),
                'ua_change_interval': self._ua_change_interval,
            }


# ==================== 全局单例 ====================

_global_session_manager: Optional[HTTPSessionManager] = None


def get_session_manager() -> HTTPSessionManager:
    """
    获取全局 HTTP Session 管理器（单例）

    Returns:
        HTTPSessionManager 实例
    """
    global _global_session_manager
    if _global_session_manager is None:
        _global_session_manager = HTTPSessionManager()
    return _global_session_manager


def get_session() -> requests.Session:
    """
    获取当前 HTTP Session（便捷函数）

    Returns:
        requests.Session 对象
    """
    return get_session_manager().get_session()


# ==================== 便捷装饰器 ====================

def with_session_refresh(func):
    """
    装饰器：在函数失败后刷新 Session

    Usage:
        @with_session_refresh
        def fetch_data(url):
            session = get_session()
            return session.get(url)
    """
    def wrapper(*args, **kwargs):
        try:
            result = func(*args, **kwargs)
            get_session_manager().record_success()
            return result
        except Exception as e:
            should_refresh = get_session_manager().record_failure()
            if should_refresh:
                get_session_manager().refresh_session()
            raise
    return wrapper


# ==================== 测试代码 ====================

if __name__ == "__main__":
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("=" * 60)
    print("HTTP Session 管理器测试")
    print("=" * 60)

    # 测试 1: 获取 Session
    print("\n[测试 1] 获取 Session...")
    mgr = get_session_manager()
    session = mgr.get_session()
    print(f"  Session 创建: {session is not None}")
    print(f"  UA: {session.headers.get('User-Agent', 'N/A')[:50]}...")

    # 测试 2: Session 复用
    print("\n[测试 2] Session 复用...")
    session2 = mgr.get_session()
    print(f"  同一实例: {session is session2}")

    # 测试 3: UA 轮换
    print("\n[测试 3] UA 轮换（生成 5 个随机 UA）...")
    uas = [mgr.get_random_ua() for _ in range(5)]
    for i, ua in enumerate(uas, 1):
        print(f"  [{i}] {ua[:60]}...")
    unique_uas = len(set(uas))
    print(f"  不同 UA 数量: {unique_uas}/5")

    # 测试 4: 状态查询
    print("\n[测试 4] 状态查询...")
    status = mgr.get_status()
    for key, value in status.items():
        print(f"  {key}: {value}")

    # 测试 5: 模拟成功/失败记录
    print("\n[测试 5] 成功/失败记录...")
    mgr.record_success()
    print(f"  记录成功后状态: {mgr.get_status()['consecutive_failures']}")
    mgr.record_failure()
    mgr.record_failure()
    mgr.record_failure()
    print(f"  记录3次失败后状态: {mgr.get_status()['consecutive_failures']}")
    should_refresh = mgr.record_failure()
    print(f"  第4次失败应刷新: {should_refresh}")

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)
