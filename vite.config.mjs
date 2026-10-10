import { defineConfig } from 'vite';
import os from 'node:os';
import path from 'node:path';

// Keep Vite's disposable optimizer cache out of node_modules/.vite, which
// OneDrive may hold as a synced placeholder on Windows.
export default defineConfig({
  cacheDir: path.join(os.tmpdir(), 'everest-textile-vite-cache'),
});
