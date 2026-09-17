import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    // 默认 node 环境（主进程模块）；渲染层测试文件用 docblock
    // `// @vitest-environment jsdom` 自行切换，避免配置层耦合
    environment: 'node',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    // Windows 下并行 worker 对 vite-node ssr 缓存文件写冲突（EBUSY），串行执行
    fileParallelism: false
  }
});
