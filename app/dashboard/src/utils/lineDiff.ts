export type DiffLine = {
  type: "same" | "add" | "del";
  text: string;
  // 1-based line numbers in the old / new text; undefined on the side the line is absent from
  oldNo?: number;
  newNo?: number;
};

export type DiffHunk = { lines: DiffLine[] };

// The table is n*m cells; past this the editor would stall, so fall back to
// "everything removed, everything added" for the part that differs.
const MAX_CELLS = 6_000_000;

/** Line diff by longest common subsequence, after trimming what both sides share at the ends. */
export const diffLines = (oldText: string, newText: string): DiffLine[] => {
  const a = oldText.split("\n");
  const b = newText.split("\n");

  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length;
  let endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) {
    endA--;
    endB--;
  }

  const out: DiffLine[] = [];
  for (let i = 0; i < start; i++)
    out.push({ type: "same", text: a[i], oldNo: i + 1, newNo: i + 1 });

  const n = endA - start;
  const m = endB - start;

  if (n * m > MAX_CELLS) {
    for (let i = 0; i < n; i++)
      out.push({ type: "del", text: a[start + i], oldNo: start + i + 1 });
    for (let j = 0; j < m; j++)
      out.push({ type: "add", text: b[start + j], newNo: start + j + 1 });
  } else if (n || m) {
    const width = m + 1;
    const table = new Uint32Array((n + 1) * width);
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        table[i * width + j] =
          a[start + i] === b[start + j]
            ? table[(i + 1) * width + j + 1] + 1
            : Math.max(table[(i + 1) * width + j], table[i * width + j + 1]);
      }
    }
    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (a[start + i] === b[start + j]) {
        out.push({
          type: "same",
          text: a[start + i],
          oldNo: start + i + 1,
          newNo: start + j + 1,
        });
        i++;
        j++;
      } else if (table[(i + 1) * width + j] >= table[i * width + j + 1]) {
        out.push({ type: "del", text: a[start + i], oldNo: start + i + 1 });
        i++;
      } else {
        out.push({ type: "add", text: b[start + j], newNo: start + j + 1 });
        j++;
      }
    }
    for (; i < n; i++)
      out.push({ type: "del", text: a[start + i], oldNo: start + i + 1 });
    for (; j < m; j++)
      out.push({ type: "add", text: b[start + j], newNo: start + j + 1 });
  }

  for (let k = 0; endA + k < a.length; k++)
    out.push({
      type: "same",
      text: a[endA + k],
      oldNo: endA + k + 1,
      newNo: endB + k + 1,
    });

  return out;
};

/** Changed lines with `context` unchanged lines around them, split where the gaps are. */
export const toHunks = (lines: DiffLine[], context = 3): DiffHunk[] => {
  const keep = new Array<boolean>(lines.length).fill(false);
  lines.forEach((line, index) => {
    if (line.type === "same") return;
    const from = Math.max(0, index - context);
    const to = Math.min(lines.length - 1, index + context);
    for (let k = from; k <= to; k++) keep[k] = true;
  });

  const hunks: DiffHunk[] = [];
  let current: DiffLine[] | null = null;
  lines.forEach((line, index) => {
    if (!keep[index]) {
      current = null;
      return;
    }
    if (!current) {
      current = [];
      hunks.push({ lines: current });
    }
    current.push(line);
  });
  return hunks;
};

export const countChanges = (lines: DiffLine[]) => ({
  added: lines.filter((l) => l.type === "add").length,
  removed: lines.filter((l) => l.type === "del").length,
});
