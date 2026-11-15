'use strict';
const crypto = require('node:crypto');
const zlib = require('node:zlib');

function crc32(bytes) {
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0);
  }
  return (crc ^ 0xffffffff) >>> 0;
}
function chunk(type, data) {
  const name = Buffer.from(type), size = Buffer.alloc(4), crc = Buffer.alloc(4);
  size.writeUInt32BE(data.length); crc.writeUInt32BE(crc32(Buffer.concat([name, data])));
  return Buffer.concat([size, name, data, crc]);
}
function challenge() {
  const expected = Array.from({ length: 6 }, () => crypto.randomInt(10)).join('');
  const width = 372, height = 108, pixels = Buffer.alloc(width * height * 3, 255);
  const segments = ['abcdef', 'bc', 'abged', 'abgcd', 'fgbc', 'afgcd', 'afgecd', 'abc', 'abcdefg', 'abfgcd'];
  const boxes = { a: [10, 5, 40, 11], b: [40, 11, 47, 46], c: [40, 53, 47, 88], d: [10, 88, 40, 95],
    e: [3, 53, 10, 88], f: [3, 11, 10, 46], g: [10, 46, 40, 53] };
  [...expected].forEach((digit, index) => {
    for (const part of segments[Number(digit)]) {
      const [x1, y1, x2, y2] = boxes[part];
      for (let y = y1 + 5; y < y2 + 5; y++) for (let x = x1 + index * 60 + 7; x < x2 + index * 60 + 7; x++) {
        const offset = (y * width + x) * 3;
        pixels[offset] = 24; pixels[offset + 1] = 36; pixels[offset + 2] = 58;
      }
    }
  });
  const rows = Buffer.concat(Array.from({ length: height }, (_, y) => Buffer.concat([Buffer.from([0]), pixels.subarray(y * width * 3, (y + 1) * width * 3)])));
  const header = Buffer.alloc(13); header.writeUInt32BE(width); header.writeUInt32BE(height, 4); header[8] = 8; header[9] = 2;
  const png = Buffer.concat([Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', header), chunk('IDAT', zlib.deflateSync(rows)), chunk('IEND', Buffer.alloc(0))]);
  return { expected, url: 'data:image/png;base64,' + png.toString('base64') };
}
module.exports = { challenge };
