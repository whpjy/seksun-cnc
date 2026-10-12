import * as THREE from "three";
import type { TurningStockSample } from "./types";

/** Close both end faces; a zero inner radius represents solid material. */
export function createTurningStockGeometry(samples: TurningStockSample[]): THREE.BufferGeometry {
  const ordered = [...samples].sort((a, b) => a.z - b.z);
  if (ordered.length < 2) return new THREE.BufferGeometry();
  if (ordered.length === 2 && ordered.every(sample => sample.inner_radius <= 1e-6)
      && Math.abs(ordered[0].outer_radius - ordered[1].outer_radius) <= 1e-6) {
    return new THREE.CylinderGeometry(ordered[0].outer_radius, ordered[0].outer_radius,
      Math.max(ordered[1].z - ordered[0].z, 1e-6), 64, 1, false)
      .translate(0, (ordered[0].z + ordered[1].z) / 2, 0);
  }
  const section = [
    ...ordered.map(sample => new THREE.Vector2(Math.max(sample.outer_radius, 0), sample.z)),
    ...ordered.slice().reverse().map(sample => new THREE.Vector2(Math.max(sample.inner_radius, 0), sample.z)),
    new THREE.Vector2(Math.max(ordered[0].outer_radius, 0), ordered[0].z),
  ];
  return new THREE.LatheGeometry(section, 64);
}
