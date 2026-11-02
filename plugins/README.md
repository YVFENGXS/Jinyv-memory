# 内置插件目录（当前不参与扫描）

插件实现已迁到独立仓库，默认搜索路径由 `config/core.toml` 的
`[plugins].search_paths` 指定：

```toml
[plugins]
search_paths = ["../Jinyv-plugins"]
```

- `E:\Jinyv-plugins\qqbot`：QQ 插件仓库（适配层）
- `E:\Jinyv-plugins\desktop-pet`：桌宠适配器仓库
- `E:\Jinyv-plugins\video-call`：视频通话适配器仓库
- dsh 是特殊接入类型，桥接在 `src/jinyv_memory/integrations/dsh.py`，不进入插件加载器

主仓库 `plugins/` 下的四个实现目录已在授权后删除，仅保留本说明文件；它们不在扫描路径中。
加载器仍支持多路径扫描，同一 `plugin id` 重复时只记录冲突，不覆盖已发现插件。
