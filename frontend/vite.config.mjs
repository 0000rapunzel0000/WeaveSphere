import { defineConfig } from 'vite';
import os from 'node:os';
import path from 'node:path';

// Keep Vite's disposable optimizer cache out of node_modules/.vite, which
// OneDrive may hold as a synced placeholder on Windows.
export default defineConfig({
  cacheDir: path.join(os.tmpdir(), 'everest-textile-vite-cache'),
  publicDir: 'public',
  // Two pages: the app, and the buyer's tracking page (with the call to the order desk).
  build: { rollupOptions: { input: { main: 'index.html', track: 'track.html' } } },
  // The app calls /api/...; in development that goes to the backend on port 8000 (or API_TARGET).
  server: {
    proxy: {
      '/api': { target: process.env.API_TARGET || 'http://127.0.0.1:8000', rewrite: (p) => p.replace(/^\/api/, '') },
    },
  },
});
