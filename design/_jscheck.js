// Syntax-check the <script data-dc-script> block of each artboard (no runtime, parse only).
const fs = require('fs');
let bad = 0;
for (const f of ['Main', 'Plan', 'Run', 'Model', 'Deliver', 'Digest']) {
  const t = fs.readFileSync(__dirname + '/' + f + '.dc.html', 'utf8');
  const m = t.match(/<script data-dc-script[^>]*>([\s\S]*?)<\/script>/);
  const src = 'class DCLogic{};' + m[1];
  try { new Function(src); console.log(f, 'js ok'); } catch (e) { bad++; console.log(f, 'JS ERROR', e.message); }
}
process.exit(bad ? 1 : 0);
