// ESLint 配置 —— C2：内联 JS 的静态检查
// ================================================================
// 为什么只开 no-undef：
//   仪表盘的内联 JS 有 88KB，历史上出过 `render()` 里 `avg 未定义` 导致
//   整页冻结（圈速列表/曲线/轨迹/G-G 全不动，控制台只有一行 ReferenceError）。
//   那正是 no-undef 能拦下的类型，而 `node --check` 只查语法、查不出来。
//
//   不开 no-unused-vars / 风格类规则：这个文件是「一个大 IIFE 里几十个函数
//   互相调用」的形态，未使用变量多半是刻意留的调试出口，一开就是几十条
//   噪声，第一天就会把 CI 关掉 —— 那等于没有门禁。
//
// 被检查的文件是**渲染后**从页面里抽出来的 <script>（见 tools/lint_inline_js.py），
// 不是源码：源码里的 HTML 是模板拼的，直接扫会一片红。
import globals from "globals";

export default [
  {
    files: ["**/*.js"],
    languageOptions: {
      ecmaVersion: 2022,
      // 页面脚本不是模块：它们共享 window 作用域，跨 <script> 互相调用。
      sourceType: "script",
      globals: {
        ...globals.browser,
        // 页面里由服务端注入或由别处定义的少量全局
        SLIP: "writable",
        EV: "writable",
        COACH: "writable",
        TRACKS: "writable",
        // 这两个在页面里是 `window.drawCmpDiff = function () {...}` 赋值的
        // （要给内联 onclick 用），随后又在同一文件里裸调用。
        // 运行时没问题（window 属性就是全局），但 ESLint 不会从赋值推断
        // 全局声明，所以在这里显式声明。
        drawCmpDiff: "writable",
        drawCmpPv: "writable",
      },
    },
    rules: {
      "no-undef": "error",
      // 🔴 不开 no-implicit-globals：这些页面的架构就是「顶层函数 + 内联
      //    onclick="foo()"」，顶层声明是**故意的**，不是漏写 var。
      //    开了它立刻 170 条全是噪声。
      // 自比较 / 自赋值这类必然写错的表达式
      "no-self-compare": "error",
      "no-self-assign": "error",
      // 在 if/for 里写函数声明（浏览器行为不一致）
      "no-inner-declarations": "error",
      // 不可达代码
      "no-unreachable": "error",
    },
  },
];
