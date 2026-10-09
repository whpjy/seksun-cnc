import { useCallback, useEffect, useRef, useState } from "react";
import type { PointerEvent } from "react";

export default function useFloatingConsole() {
  const ref = useRef<HTMLElement>(null);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const drag = useRef<{ id: number; x: number; y: number } | null>(null);
  const move = useCallback((dx: number, dy: number) => {
    const element = ref.current;
    const parent = element?.parentElement;
    if (!element || !parent) return;
    const box = element.getBoundingClientRect(), bounds = parent.getBoundingClientRect();
    const x = Math.max(bounds.left + 8 - box.left, Math.min(dx, bounds.right - 8 - box.right));
    const y = Math.max(bounds.top + 8 - box.top, Math.min(dy, bounds.bottom - 32 - box.bottom));
    setOffset(previous => ({ x: previous.x + x, y: previous.y + y }));
  }, []);
  useEffect(() => {
    const element = ref.current;
    if (!element?.parentElement) return;
    // Keep the window reachable after resizing or expanding its contents.
    let frame = 0;
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => move(0, 0));
    });
    observer.observe(element);
    observer.observe(element.parentElement);
    return () => { observer.disconnect(); cancelAnimationFrame(frame); };
  }, [move]);
  const onPointerDown = (event: PointerEvent<HTMLElement>) => {
    if (event.button !== 0 || (event.target as HTMLElement).closest("button,a,input")) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY };
  };
  const onPointerMove = (event: PointerEvent<HTMLElement>) => {
    const previous = drag.current;
    if (!previous || previous.id !== event.pointerId) return;
    move(event.clientX - previous.x, event.clientY - previous.y);
    drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY };
  };
  const end = () => { drag.current = null; };
  return { ref, style: { transform: `translate(${offset.x}px, ${offset.y}px)` }, headerProps: { onPointerDown, onPointerMove, onPointerUp: end, onPointerCancel: end, onLostPointerCapture: end } };
}
