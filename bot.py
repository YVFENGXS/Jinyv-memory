# -*- coding: utf-8 -*-
"""NoneBot2 启动入口：python bot.py

先复制 .env.example 为 .env 并填好 ONEBOT_ACCESS_TOKEN / RP_FRONT_KEY / RP_BACK_KEY。
"""
import nonebot
from nonebot.adapters.onebot.v11 import Adapter

nonebot.init()
nonebot.get_driver().register_adapter(Adapter)
nonebot.load_plugin("jinyu_memory")

if __name__ == "__main__":
    nonebot.run()
