// Store authoritative negative decisions as well as positive ones.
export class DecisionCache {
  constructor(policy, read, write) {
    this.policy = policy;
    this.read = read;
    this.write = write;
    this.entries = new Map();
  }

  get(pid) {
    if (!this.entries.has(pid)) {
      try {
        const value = this.read(pid);
        if (value && value.policy === this.policy && ['allowed', 'denied'].includes(value.decision)) {
          this.entries.set(pid, value);
        }
      } catch (_) { /* Corrupt cache never grants entry. */ }
    }
    return this.entries.get(pid) || { decision: 'unknown', revision: -1, policy: this.policy };
  }

  accept(value) {
    if (!value || typeof value.pid !== 'string' || value.policy !== this.policy ||
        !Number.isSafeInteger(value.revision) || value.revision < 0 ||
        !['allowed', 'denied', 'unlinked', 'unknown'].includes(value.decision)) {
      throw new Error('Invalid authorization response');
    }
    const old = this.get(value.pid);
    if (value.revision < old.revision) return old;
    const entry = {decision: value.decision === 'allowed' ? 'allowed' : 'denied',
      revision: value.revision, policy: this.policy};
    this.entries.set(value.pid, entry);
    this.write(value.pid, entry);
    return entry;
  }
}
