/**
 * 构建产物样式断言（护栏 1，fix-tailwind-build-pipeline）
 *
 * 扫描 out/renderer/assets/*.css，断言：
 *  1. 无 `@tailwind` / `@apply` 指令字面量残留（未编译症状——本缺陷的根因指纹）
 *  2. 存在 `.bg-base` 工具类规则（tailwind.config 已生成 utilities 的证据）
 *  3. CSS 总体量达到完整规模（>= 10KB，防止"编译了但工具类为空"的退化）
 *
 * 用法：node scripts/check-built-css.cjs（npm run check:css）
 */
const fs = require('fs');
const path = require('path');

const ASSETS = path.resolve(__dirname, '..', 'out', 'renderer', 'assets');
const MIN_TOTAL_BYTES = 10 * 1024;

const results = [];
let exitCode = 0;
const check = (name, ok, extra) => {
  results.push(`${ok ? 'PASS' : 'FAIL'} | ${name}${extra ? ` (${extra})` : ''}`);
  if (!ok) exitCode = 1;
};

function main() {
  if (!fs.existsSync(ASSETS)) {
    check('产物目录存在', false, ASSETS);
    return;
  }

  const files = fs.readdirSync(ASSETS).filter((f) => f.endsWith('.css'));
  check('存在 CSS 产物', files.length > 0, `${files.length} files`);

  let totalBytes = 0;
  let hasRawDirective = false;
  let hasBgBase = false;

  for (const f of files) {
    const text = fs.readFileSync(path.join(ASSETS, f), 'utf8');
    const size = Buffer.byteLength(text, 'utf8');
    totalBytes += size;
    if (text.includes('@tailwind') || text.includes('@apply')) hasRawDirective = true;
    if (text.includes('.bg-base')) hasBgBase = true;
    results.push(`INFO | ${f} (${size} bytes)`);
  }

  check('无 @tailwind/@apply 指令残留（样式已编译）', !hasRawDirective);
  check('包含 .bg-base 根背景工具类规则', hasBgBase);
  check(
    `CSS 总体量 >= ${MIN_TOTAL_BYTES / 1024}KB（完整规模）`,
    totalBytes >= MIN_TOTAL_BYTES,
    `${(totalBytes / 1024).toFixed(1)} KB`
  );
}

main();
console.log(results.join('\n'));
console.log(`CHECK_BUILT_CSS_DONE exitCode=${exitCode}`);
process.exit(exitCode);
