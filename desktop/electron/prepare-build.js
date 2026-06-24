const fs = require('fs');
const path = require('path');
const repoRoot = path.resolve(__dirname, '..', '..');
fs.writeFileSync(path.join(__dirname, 'repo-path.json'), JSON.stringify({ repoRoot }, null, 2));
console.log(`Wrote repo-path.json -> ${repoRoot}`);
