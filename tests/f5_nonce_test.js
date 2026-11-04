const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const sourcePath = path.join(__dirname, '..', 'static', 'js', 'app.js');
const source = fs.readFileSync(sourcePath, 'utf8');
let calls = 0;
const sandbox = {
  window: {
    crypto: {
      getRandomValues(bytes) {
        const seed = calls++;
        for (let index = 0; index < bytes.length; index += 1) bytes[index] = (seed + index) & 0xff;
        return bytes;
      },
    },
  },
  document: { addEventListener() {} },
  console,
};
vm.runInNewContext(source, sandbox, { filename: sourcePath });

const first = sandbox.randomNonce();
const second = sandbox.randomNonce();
assert.equal(first.length, 64);
assert.equal(second.length, 64);
assert.notEqual(first, second);
assert.equal(calls, 2);

sandbox.window.crypto = {};
assert.throws(() => sandbox.randomNonce(), /安全随机数/);
assert.equal(calls, 2);
console.log(`F5_NONCE_OK secure_calls=${calls} distinct=1 unsupported=throws`);
