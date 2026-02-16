/**
 * Pre-warmed pool of Pyodide instances.
 *
 * Each instance has polars pre-loaded. All indicator computation
 * is done with pure Polars operations (no external TA library).
 * Instances are acquired before use and released back after execution.
 * Globals are reset between uses to prevent state leakage.
 */

import { loadPyodide, type PyodideInterface } from "pyodide";

export interface PoolOptions {
  size: number;
  packages: string[];
}

export class PyodidePool {
  private available: PyodideInterface[] = [];
  private waiters: Array<(py: PyodideInterface) => void> = [];
  private size: number;
  private packages: string[];
  private ready = false;

  constructor(opts: PoolOptions) {
    this.size = opts.size;
    this.packages = opts.packages;
  }

  async init(): Promise<void> {
    console.log(`[pool] Warming ${this.size} Pyodide instance(s)...`);
    const start = Date.now();

    const promises = Array.from({ length: this.size }, () =>
      this.createInstance()
    );
    this.available = await Promise.all(promises);
    this.ready = true;

    const elapsed = ((Date.now() - start) / 1000).toFixed(1);
    console.log(
      `[pool] ${this.size} instance(s) ready in ${elapsed}s ` +
        `(packages: ${this.packages.join(", ")})`
    );
  }

  private async createInstance(): Promise<PyodideInterface> {
    const py = await loadPyodide();
    await py.loadPackage("micropip");

    const micropip = py.pyimport("micropip");
    for (const pkg of this.packages) {
      try {
        await micropip.install(pkg);
      } catch (err) {
        console.warn(`[pool] Failed to install ${pkg}: ${err}`);
      }
    }

    // Pre-import polars and math
    py.runPython(`
import polars as pl
import math
`);

    return py;
  }

  async acquire(): Promise<PyodideInterface> {
    if (this.available.length > 0) {
      return this.available.pop()!;
    }
    // Wait for one to be released
    return new Promise<PyodideInterface>((resolve) => {
      this.waiters.push(resolve);
    });
  }

  release(py: PyodideInterface): void {
    // Reset user globals to prevent state leakage between bots
    py.runPython(`
_keep = {'pl', 'math', 'polars', '__builtins__', '__name__', '__doc__', '__package__', '__loader__', '__spec__'}
_to_del = [k for k in list(globals().keys()) if k not in _keep and not k.startswith('_')]
for _k in _to_del:
    del globals()[_k]
del _keep, _to_del
try:
    del _k
except NameError:
    pass
`);

    if (this.waiters.length > 0) {
      const resolve = this.waiters.shift()!;
      resolve(py);
    } else {
      this.available.push(py);
    }
  }

  get status() {
    return {
      ready: this.ready,
      total: this.size,
      available: this.available.length,
      waiting: this.waiters.length,
    };
  }
}
