# -*- coding: utf-8 -*-
"""运行时异常：区分配置、插件、记忆与租约四类，便于插件降级时按类型处理。"""


class JinyvError(Exception):
    """所有运行时异常的基类。"""


class ConfigError(JinyvError):
    """配置缺失、类型错误或路径不可用。"""


class PluginError(JinyvError):
    """插件注册或调用失败。"""


class PluginLoadError(PluginError):
    """插件模块无法导入或入口不合法。"""


class PluginDependencyError(PluginError):
    """插件依赖缺失、成环或被禁用。"""


class MemoryError(JinyvError):
    """记忆读写失败。"""


class LeaseBusy(JinyvError):
    """租约已被其他执行者持有。"""
