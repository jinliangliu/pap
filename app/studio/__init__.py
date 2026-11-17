# app/studio/__init__.py
"""从文件导入：解析层 + 导入编排。

`parsers` 是纯函数（文件字节 → 草稿），`importer` 负责权限判定与写库。
分开的理由是前者能被单测直接喂字节，不用起数据库。

入口只有一条：构建助手抽屉的加号（`studio_import` 工具），它按附件句柄取磁盘
原文再调这里的 `run_import`。`POST /api/v1/studio/import` 是同一套逻辑的 HTTP
形态，留给脚本与 `dry_run` 预览；控制台自己不再调用它。
"""
