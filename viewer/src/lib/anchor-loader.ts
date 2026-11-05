import type { Anchor } from "../api/types";

type Pending = { promise: Promise<void>; done: () => void };
type Scope = {
  key: string;
  cached: Set<string>;
  pending: Map<string, Pending>;
  queue: Set<string>;
  running: number;
};

/** 合并同一轮卡片的来源请求；每批 200 条、至多两批在途。缓存随教材版本失效。 */
export class AnchorLoader {
  private scope?: Scope;

  constructor(
    private load: (key: string, ids: string[]) => Promise<Anchor[]>,
    private publish: (rows: Anchor[]) => void,
  ) {}

  reset(key: string, force = false): void {
    if (!force && this.scope?.key === key) return;
    if (this.scope) {
      for (const pending of this.scope.pending.values()) pending.done();
      this.scope.queue.clear();
    }
    this.scope = { key, cached: new Set(), pending: new Map(), queue: new Set(), running: 0 };
  }

  async fetch(ids: string[]): Promise<void> {
    const scope = this.scope;
    if (!scope) return;
    const waiting = [];
    for (const id of new Set(ids.filter(Boolean))) {
      if (scope.cached.has(id)) continue;
      let pending = scope.pending.get(id);
      if (!pending) {
        let done!: () => void;
        const promise = new Promise<void>((resolve) => { done = resolve; });
        pending = { promise, done };
        scope.pending.set(id, pending);
        scope.queue.add(id);
      }
      waiting.push(pending.promise);
    }
    queueMicrotask(() => this.drain(scope));
    await Promise.all(waiting);
  }

  private drain(scope: Scope): void {
    if (scope !== this.scope) return;
    while (scope.running < 2 && scope.queue.size) {
      const ids = [...scope.queue].slice(0, 200);
      ids.forEach((id) => scope.queue.delete(id));
      scope.running++;
      void Promise.resolve().then(() => this.load(scope.key, ids)).then((rows) => {
        if (scope !== this.scope) return;
        // 缺失的来源也记到本版本，避免卡片反复挂载造成重复查询。
        ids.forEach((id) => scope.cached.add(id));
        this.publish(rows);
      }).catch(() => {
        // 网络失败不记缓存；下次请求可重试，卡片暂时显示未知页码。
      }).finally(() => {
        for (const id of ids) {
          scope.pending.get(id)?.done();
          scope.pending.delete(id);
        }
        scope.running--;
        this.drain(scope);
      });
    }
  }
}
