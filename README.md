# texture-mip-streaming

贴图流送（mip 规划 / 显存预算 / LRU 淘汰）Bugfix 题。初始状态下
`tests/test_streaming.py` 的 15 个测试全部失败，需要实现
`texture/streaming.py` 中的 8 个函数。

## 题面

开放世界一进大场景，贴图就糊成一片还爆显存：加载只按请求顺序进行，完全不看优先级，高优先级的贴图反而被挤出；预算按纹理数量而不是字节数核算，直到爆了显存才报错；淘汰按加入顺序（FIFO）进行，正在使用的纹理也会被踢掉；升级 mip 前不做预算检查，先加载再淘汰，导致瞬间超限；缺失 mip 时直接回退到 0 级，显存翻 4 倍；字节对齐向下取整，DDS 行宽错位；resolve_mip 不设下限，128 的 8 级算出 0；覆盖率与距离未归一，优先级分数超出 0..1；批量流送遇到损坏纹理只跳过，输出里留下半加载状态；同一纹理重复请求不去重，出现两份驻留记录。texture/streaming.py 是核心，tests/test_streaming.py 15个红。要修的是：按优先级而非请求顺序装载、按字节而非纹理数量核算预算、LRU 淘汰并保护在用纹理、升级前先校验预算、缺失 mip 回退到可用级别、字节向上对齐、resolve_mip 结果下限为 1、覆盖率与距离归一化到 0..1、批量损坏时整体回滚、同一纹理去重。

## 运行方式

```bash
python -m pytest tests -q
```

初始状态应恰好输出 `15 failed`，没有 collection/import/syntax error；
修好 `texture/streaming.py` 后应输出 `15 passed`。

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 游戏、图形与多媒体 |
| 子方向 | 图形渲染 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## 仓库结构

```
texture-streaming/
  README.md
  conftest.py
  .gitignore
  texture/__init__.py
  texture/streaming.py
  tests/test_streaming.py
```

## 约束

- `tests/test_streaming.py` 不可修改。
- 只使用 Python 标准库（heapq、math、collections）与 pytest；离线、无随机。
- 只需实现 `texture/streaming.py` 中的 8 个函数，公开接口与 docstring 契约保持不变。
