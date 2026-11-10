// 極簡 zip 讀寫（零依賴）。支援 stored / deflate，中文檔名：UTF-8 旗標或 Windows 舊版的 Big5（CP950）。
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';

const big5 = (() => { try { return new TextDecoder('big5'); } catch { return null; } })();
function decodeName(buf, utf8Flag) {
  if (utf8Flag) return buf.toString('utf8');
  if (!buf.some((b) => b > 0x7f)) return buf.toString('latin1');
  // 沒有 UTF-8 旗標但有非 ASCII：先試 UTF-8（很多工具不設旗標），失敗再用 Big5
  const u = buf.toString('utf8');
  if (!u.includes('�')) return u;
  return big5 ? big5.decode(buf) : u;
}

export function readZip(buf) {
  let eocd = -1;
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 65557); i--) if (buf.readUInt32LE(i) === 0x06054b50) { eocd = i; break; }
  if (eocd < 0) throw new Error('不是有效的 zip 檔');
  const count = buf.readUInt16LE(eocd + 10);
  let p = buf.readUInt32LE(eocd + 16);
  const entries = [];
  for (let n = 0; n < count; n++) {
    if (buf.readUInt32LE(p) !== 0x02014b50) throw new Error('zip 目錄損壞');
    const flags = buf.readUInt16LE(p + 8), method = buf.readUInt16LE(p + 10);
    const csize = buf.readUInt32LE(p + 20), usize = buf.readUInt32LE(p + 24);
    const nlen = buf.readUInt16LE(p + 28), elen = buf.readUInt16LE(p + 30), clen = buf.readUInt16LE(p + 32);
    const local = buf.readUInt32LE(p + 42);
    const name = decodeName(buf.subarray(p + 46, p + 46 + nlen), flags & 0x800).replace(/\\/g, '/');
    entries.push({ name, method, csize, usize, local, dir: name.endsWith('/') });
    p += 46 + nlen + elen + clen;
  }
  return entries.map((e) => ({
    ...e,
    data() {
      const lp = e.local;
      if (buf.readUInt32LE(lp) !== 0x04034b50) throw new Error('zip 檔案資料損壞');
      const start = lp + 30 + buf.readUInt16LE(lp + 26) + buf.readUInt16LE(lp + 28);
      const raw = buf.subarray(start, start + e.csize);
      if (e.method === 0) return Buffer.from(raw);
      if (e.method === 8) return zlib.inflateRawSync(raw);
      throw new Error(`不支援的壓縮方式（${e.method}）：${e.name}`);
    },
  }));
}

// 解壓縮到 dest；stripTop = 若所有檔案都在同一個最上層資料夾，去掉那一層
export function extractZip(buf, dest, { stripTop = false, skip = () => false } = {}) {
  const entries = readZip(buf).filter((e) => !/(^|\/)(__MACOSX|\.DS_Store)(\/|$)/.test(e.name));
  let prefix = '';
  if (stripTop) {
    const tops = new Set(entries.map((e) => e.name.split('/')[0]));
    if (tops.size === 1 && entries.some((e) => e.name.includes('/'))) prefix = [...tops][0] + '/';
  }
  const root = path.resolve(dest);
  let files = 0;
  for (const e of entries) {
    let rel = e.name.startsWith(prefix) ? e.name.slice(prefix.length) : e.name;
    if (!rel || skip(rel)) continue;
    const out = path.resolve(root, rel);
    if (out !== root && !out.startsWith(root + path.sep)) continue; // 防止跳出資料夾
    if (e.dir) { fs.mkdirSync(out, { recursive: true }); continue; }
    fs.mkdirSync(path.dirname(out), { recursive: true });
    fs.writeFileSync(out, e.data());
    files++;
  }
  return { files, top: prefix ? prefix.slice(0, -1) : null };
}

export function zipTopFolder(buf) {
  const tops = new Set(readZip(buf).map((e) => e.name.split('/')[0]).filter(Boolean));
  return tops.size === 1 ? [...tops][0] : null;
}

// 把資料夾打包成 zip（UTF-8 檔名，Windows 檔案總管看得懂）
export function zipFolder(dir, { baseName = path.basename(dir), skip = () => false } = {}) {
  const locals = [], centrals = []; let offset = 0;
  const walk = (rel) => {
    for (const ent of fs.readdirSync(path.join(dir, rel), { withFileTypes: true })) {
      const r = rel ? `${rel}/${ent.name}` : ent.name;
      if (skip(r, ent)) continue;
      if (ent.isDirectory()) walk(r);
      else if (ent.isFile()) add(`${baseName}/${r}`, fs.readFileSync(path.join(dir, r)), fs.statSync(path.join(dir, r)).mtime);
    }
  };
  const add = (name, data, mtime) => {
    const nameBuf = Buffer.from(name, 'utf8');
    const comp = zlib.deflateRawSync(data, { level: 6 });
    const useStore = comp.length >= data.length;
    const body = useStore ? data : comp;
    const crc = zlib.crc32(data) >>> 0;
    const d = mtime || new Date();
    const dosTime = (d.getHours() << 11) | (d.getMinutes() << 5) | (d.getSeconds() >> 1);
    const dosDate = ((d.getFullYear() - 1980) << 9) | ((d.getMonth() + 1) << 5) | d.getDate();
    const lh = Buffer.alloc(30);
    lh.writeUInt32LE(0x04034b50, 0); lh.writeUInt16LE(20, 4); lh.writeUInt16LE(0x800, 6); lh.writeUInt16LE(useStore ? 0 : 8, 8);
    lh.writeUInt16LE(dosTime, 10); lh.writeUInt16LE(dosDate, 12); lh.writeUInt32LE(crc, 14);
    lh.writeUInt32LE(body.length, 18); lh.writeUInt32LE(data.length, 22); lh.writeUInt16LE(nameBuf.length, 26); lh.writeUInt16LE(0, 28);
    const ch = Buffer.alloc(46);
    ch.writeUInt32LE(0x02014b50, 0); ch.writeUInt16LE(20, 4); ch.writeUInt16LE(20, 6); ch.writeUInt16LE(0x800, 8); ch.writeUInt16LE(useStore ? 0 : 8, 10);
    ch.writeUInt16LE(dosTime, 12); ch.writeUInt16LE(dosDate, 14); ch.writeUInt32LE(crc, 16); ch.writeUInt32LE(body.length, 20); ch.writeUInt32LE(data.length, 24);
    ch.writeUInt16LE(nameBuf.length, 28); ch.writeUInt32LE(offset, 42);
    locals.push(lh, nameBuf, body); centrals.push(ch, nameBuf);
    offset += 30 + nameBuf.length + body.length;
  };
  walk('');
  const cd = Buffer.concat(centrals);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(centrals.length / 2, 8); end.writeUInt16LE(centrals.length / 2, 10);
  end.writeUInt32LE(cd.length, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, cd, end]);
}
