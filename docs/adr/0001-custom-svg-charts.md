# ADR-0001：前端图表用手写 SVG，不引 recharts

- 状态：Accepted
- 日期：2026-09-06
- 相关：`frontend/src/components/charts/`、`frontend/vite.config.ts`

## Context

操作台需要四类可视化：KPI 趋势线、迷你走势（sparkline）、状态占比环图、排行榜条形。数量不多，形态固定，而且都在暗色主题的卡片里，需要精确控制描边、网格、tooltip 与动画。

候选方案：

1. **recharts** — React 生态最常用，声明式，覆盖绝大多数图表类型。
2. **visx / d3** — 更底层，自由度高，但等于自己写。
3. **手写 SVG 组件** — 只实现真正需要的四种。

约束条件：

- 前端最终由 nginx 提供静态资源，首屏体积直接影响操作台的可用性（投放人员经常在网络一般的办公室打开它）。
- 图表种类是**封闭集合**，短期不会新增雷达图、桑基图之类。
- 团队需要能读懂并修改图表行为（例如"预算变化超过阈值时把折线染红"），而不是绕过库的抽象去 hack。

## Decision

手写四个 SVG 组件 + 一个纯函数工具模块：

```
components/charts/
  TrendChart.tsx     折线/面积，支持多序列、阈值线、hover tooltip
  Sparkline.tsx      卡片内的迷你走势，无坐标轴
  Donut.tsx          环形占比
  BarList.tsx        排行榜条形
  chartUtils.ts      scale/path/格式化等纯函数（有独立单测）
  chartUtils.test.ts
```

所有几何计算集中在 `chartUtils.ts`，是**纯函数**，因此可以在 jsdom 里直接单测，不需要渲染。组件本身只负责把算好的坐标画出来。

同时从 `vite.config.ts` 的 `manualChunks` 里移除 `charts: ["recharts"]`——留着它会让 `npm run build` 去解析一个根本不存在的依赖。

## Consequences

**正面**

- 前端少一个 ~120KB gzip 的运行时依赖，`charts` chunk 直接消失。
- 暗色主题、描边宽度、网格密度、tooltip 位置全部可控，不需要用 CSS 覆盖库的内部类名。
- 图表逻辑可单测。`chartUtils.test.ts` 覆盖 scale 计算、path 生成、边界情况（单点、空数组、全等值）。
- 无版本升级风险（recharts 在 React 19 上的兼容性需要单独验证）。

**负面**

- 需要自己实现坐标轴刻度、hover 命中检测、响应式宽度（用 `useMeasure` hook + ResizeObserver）。这些代码已经写完，但**维护责任在我们**。
- 新增图表类型的成本比"引个库"高。如果未来需要十几种图表，这个决定应该被重新评估并 supersede。
- 无障碍需要自己处理：SVG 要显式加 `role="img"` 与 `<title>`，键盘用户拿不到 tooltip。当前实现只做到了前者。

**中性**

- `package.json` 里没有任何图表库，新人第一眼会以为图表是图片。目录名 `components/charts/` 与文件内注释用于消解这一点。

## 复审触发条件

出现以下任一情况时重新评估：

- 需要的图表类型超过 8 种
- 需要交互式下钻（框选缩放、联动筛选）
- 需要服务端渲染图表为图片用于报表导出