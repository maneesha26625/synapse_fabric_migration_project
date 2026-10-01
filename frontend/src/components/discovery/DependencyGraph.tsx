import { Maximize2, Minus, Plus } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { GraphNode } from "../../types";
import { Button } from "../shared/Shared";

const NODE_W = 188;
const NODE_H = 30;
const COL_GAP = 130;
const ROW_GAP = 10;
const HEADER = 34;

interface Props {
  nodes: GraphNode[];
  edges: { source: string; target: string }[];
  selectedId: string | null;
  onSelect: (id: string | null) => void;
}

interface Placed extends GraphNode { x: number; y: number }

/**
 * A layered dependency graph: one column per migration wave, left to right,
 * with each edge drawn from an object to what it needs. Pan by dragging, zoom
 * with the wheel or the buttons. Drawn as plain SVG, so there is no layout
 * library; the caller caps how many nodes are passed in.
 */
export function DependencyGraph({ nodes, edges, selectedId, onSelect }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const [view, setView] = useState({ x: 16, y: 16, k: 1 });
  const drag = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null);

  const { placed, waves, width, height } = useMemo(() => {
    const byWave = new Map<number, GraphNode[]>();
    for (const n of nodes) byWave.set(n.wave, [...(byWave.get(n.wave) ?? []), n]);
    const waves = [...byWave.keys()].sort((a, b) => a - b);
    const placed = new Map<string, Placed>();
    let tallest = 0;
    waves.forEach((w, col) => {
      const list = (byWave.get(w) ?? []).sort((a, b) => a.type.localeCompare(b.type) || a.name.localeCompare(b.name));
      list.forEach((n, row) => placed.set(n.id, { ...n, x: col * (NODE_W + COL_GAP), y: HEADER + row * (NODE_H + ROW_GAP) }));
      tallest = Math.max(tallest, list.length);
    });
    return { placed, waves, width: waves.length * (NODE_W + COL_GAP), height: HEADER + tallest * (NODE_H + ROW_GAP) };
  }, [nodes]);

  const fit = useCallback(() => {
    const el = box.current;
    if (!el || !width) return;
    const k = Math.min(1, (el.clientWidth - 32) / width, (el.clientHeight - 32) / Math.max(height, 1));
    setView({ x: 16, y: 16, k: Math.max(0.2, k) });
  }, [width, height]);
  useEffect(() => { fit(); }, [fit, nodes.length]);

  // Wheel zoom needs preventDefault, which React's passive onWheel cannot do.
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const px = e.clientX - rect.left, py = e.clientY - rect.top;
      setView((v) => {
        const k = Math.max(0.15, Math.min(2.5, v.k * (e.deltaY < 0 ? 1.12 : 1 / 1.12)));
        return { k, x: px - ((px - v.x) / v.k) * k, y: py - ((py - v.y) / v.k) * k };
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const zoom = (factor: number) => setView((v) => ({ ...v, k: Math.max(0.15, Math.min(2.5, v.k * factor)) }));

  const visibleEdges = edges.filter((e) => placed.has(e.source) && placed.has(e.target));
  const connected = new Set<string>();
  if (selectedId) {
    connected.add(selectedId);
    for (const e of visibleEdges) if (e.source === selectedId || e.target === selectedId) { connected.add(e.source); connected.add(e.target); }
  }

  const path = (s: Placed, t: Placed) => {
    const sx = s.x, sy = s.y + NODE_H / 2, tx = t.x + NODE_W, ty = t.y + NODE_H / 2;
    if (t.x >= s.x) { // same column: loop out and back
      const o = NODE_W + 38;
      return `M ${s.x + NODE_W} ${sy} C ${s.x + o} ${sy}, ${t.x + o} ${ty}, ${t.x + NODE_W} ${ty}`;
    }
    const dx = Math.max(40, (sx - tx) / 2);
    return `M ${sx} ${sy} C ${sx - dx} ${sy}, ${tx + dx} ${ty}, ${tx} ${ty}`;
  };

  return (
    <div
      className="graph"
      ref={box}
      onPointerDown={(e) => { if ((e.target as Element).closest(".gn")) return; drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y }; (e.currentTarget as Element).setPointerCapture(e.pointerId); }}
      onPointerMove={(e) => { const d = drag.current; if (d) setView((v) => ({ ...v, x: d.vx + e.clientX - d.x, y: d.vy + e.clientY - d.y })); }}
      onPointerUp={() => { drag.current = null; }}
      onClick={(e) => { if (!(e.target as Element).closest(".gn")) onSelect(null); }}
    >
      <div className="graph-controls">
        <Button size="small" icon aria-label="Zoom in" onClick={() => zoom(1.25)}><Plus size={15} /></Button>
        <Button size="small" icon aria-label="Zoom out" onClick={() => zoom(0.8)}><Minus size={15} /></Button>
        <Button size="small" icon aria-label="Fit to view" onClick={fit}><Maximize2 size={14} /></Button>
      </div>
      <svg width="100%" height="100%" role="img" aria-label="Dependency graph. Columns are migration waves; arrows point to what an object needs.">
        <defs>
          <marker id="arrow" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L8,4 L0,8 z" fill="currentColor" />
          </marker>
        </defs>
        <g transform={`translate(${view.x} ${view.y}) scale(${view.k})`}>
          {waves.map((w, col) => (
            <text key={w} x={col * (NODE_W + COL_GAP)} y={16} className="gw">Wave {w}</text>
          ))}
          <g className="ge">
            {visibleEdges.map((e) => {
              const s = placed.get(e.source) as Placed, t = placed.get(e.target) as Placed;
              const hot = selectedId !== null && (e.source === selectedId || e.target === selectedId);
              return <path key={`${e.source}>${e.target}`} d={path(s, t)} className={hot ? "hot" : selectedId ? "dim" : ""} markerEnd="url(#arrow)" fill="none" />;
            })}
          </g>
          {[...placed.values()].map((n) => (
            <g
              key={n.id}
              className={`gn cls-${n.classification.toLowerCase().replace(" ", "-")}${selectedId === n.id ? " sel" : ""}${selectedId && !connected.has(n.id) ? " dim" : ""}`}
              transform={`translate(${n.x} ${n.y})`}
              tabIndex={0}
              role="button"
              aria-label={`${n.name}, ${n.type}, wave ${n.wave}`}
              onClick={(ev) => { ev.stopPropagation(); onSelect(n.id); }}
              onKeyDown={(ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); onSelect(n.id); } }}
            >
              <rect width={NODE_W} height={NODE_H} rx={8} />
              <text x={10} y={19}>{n.name.length > 24 ? `${n.name.slice(0, 23)}…` : n.name}</text>
              <title>{`${n.name} (${n.type})`}</title>
            </g>
          ))}
        </g>
      </svg>
    </div>
  );
}
