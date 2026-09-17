/**
 * design-system spec "源码无散落硬编码"：
 * 渲染层源码（.ts/.tsx/.css）禁止出现十六进制或 rgb 函数形式的颜色字面量，
 * 唯一豁免：styles/tokens.css（token 定义文件）。
 */
import { describe, it, expect } from 'vitest';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const RENDERER_SRC = path.resolve(HERE, '..');
const EXEMPT = new Set([path.join(RENDERER_SRC, 'styles', 'tokens.css')]);
const EXTENSIONS = new Set(['.ts', '.tsx', '.css']);

const HEX_RE = /#(?:[0-9a-fA-F]{3,8})\b/g;
const RGB_RE = /\brgba?\s*\(/g;

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      walk(full, out);
    } else if (EXTENSIONS.has(path.extname(entry.name)) && !EXEMPT.has(full)) {
      out.push(full);
    }
  }
  return out;
}

describe('color token 唯一来源', () => {
  it('渲染层源码（tokens.css 除外）无硬编码颜色字面量', () => {
    const files = walk(RENDERER_SRC);
    expect(files.length).toBeGreaterThan(0);

    const violations: string[] = [];
    for (const file of files) {
      const lines = fs.readFileSync(file, 'utf8').split('\n');
      lines.forEach((line, i) => {
        for (const m of line.matchAll(HEX_RE)) {
          violations.push(`${path.relative(RENDERER_SRC, file)}:${i + 1} hex ${m[0]}`);
        }
        if (RGB_RE.test(line)) {
          violations.push(`${path.relative(RENDERER_SRC, file)}:${i + 1} rgb/rgba`);
        }
      });
    }
    expect(violations).toEqual([]);
  });
});
