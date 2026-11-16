# app/studio/__init__.py
"""Agent Studio 的「从文件新建」：解析层 + 导入编排。

`parsers` 是纯函数（文件字节 → 草稿），`importer` 负责权限判定与写库。
分开的理由是前者能被单测直接喂字节，不用起数据库。
"""
