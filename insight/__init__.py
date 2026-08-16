"""Chat Insight 核心逻辑包。

本包为纯 Python 实现，不 import astrbot，可独立于 AstrBot 进行单元测试。
分层：service（业务聚合）→ repository（全部 SQL）→ db（只读连接），
timeutil 是唯一的时间处理实现，textproc 负责分词/清洗，render 负责输出渲染。

合并自 astrbot_plugin_chat_statistics 与 astrbot_plugin_user_profile，
数据来源唯一：ChatLogger 的 chatlog.db（只读，契约见其 QUERY_GUIDE.md）。
"""
