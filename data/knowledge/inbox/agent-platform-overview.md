
# Personal Agent Platform

本项目是个人智能体平台。

## 技术选型

后端使用 FastAPI，工作流使用 LangGraph。
第一阶段使用 SQLite 或 PostgreSQL 保存会话状态。
知识库使用 BM25 关键词检索（jieba 分词），检索前先做权限过滤。

## 安全要求

密钥不得写入知识库。
写操作应先生成预览，用户确认后才能执行。
工具调用失败时，不得将失败描述为成功。
