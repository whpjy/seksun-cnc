import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { RoomEnvironment } from "three/examples/jsm/environments/RoomEnvironment.js";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { toCreasedNormals } from "three/examples/jsm/utils/BufferGeometryUtils.js";
import type { FixtureComponent, FormingPreview, ManufacturingFeature, SimulationResult, SpatialDefectRegion, SpatialDefectSample, ToolpathSegment, TurningStageView, Vec3 } from "./types";

type ViewMode = "特征" | "工艺" | "刀路" | "仿真";

type Props = {
  modelUrl: string;
  features: ManufacturingFeature[];
  selectedFeatureIds: string[];
  onSelectFeature: (id: string) => void;
  toolpathSegments?: ToolpathSegment[];
  materialSnapshotUrls?: string[];
  materialSnapshotStages?: { operationId: string; start: number; count: number }[];
  initialToolpathSegments?: ToolpathSegment[];
  profileBoundaries?: { operation_id: string; setup_id: string; work_axis: { x: number; y: number; z: number }; points: { x: number; y: number; z: number }[] }[];
  simulation?: SimulationResult | null;
  simulationBlocked?: boolean;
  turningStage?: TurningStageView | null;
  camoticsSurface?: { url: string; frame: { x: Vec3; y: Vec3; z: Vec3 } } | null;
  fixtureComponents?: FixtureComponent[];
  animateToolpath?: boolean;
  initialProgress?: number;
  playbackResetToken?: number;
  operationTools?: Record<string, { name: string; tool_name: string; diameter_mm: number; stickout_mm: number; holder_diameter_mm: number; kind: string; drill_point_angle_deg: number; spindle_rpm: number; feed_rate_mm_min: number }>;
  topologyEdges?: Vec3[][];
  activeOperationId?: string;
  isFinalOperation?: boolean;
  toolpathLoaded?: boolean;
  playbackMode?: "single" | "cumulative";
  onPlaybackModeChange?: (mode: "single" | "cumulative") => void;
  formingPreview?: FormingPreview | null;
  spatialDefects?: { regions: SpatialDefectRegion[]; samples: SpatialDefectSample[] } | null;
  onSelectDefect?: (region: SpatialDefectRegion) => void;
  viewMode?: ViewMode;
  workAxis?: Vec3 | null;
  activeOperationLabel?: string;
};

// Siemens NX/UG-style neutral blue-gray: dark enough to preserve the part's
// silhouette while still allowing the lighting to describe fillets and ribs.
const UG_PART_COLOR = 0x6f7b7d;
const UG_TARGET_COLOR = 0x788689;
const UG_EDGE_COLOR = 0x303a3d;

function featureMarkerColor(feature: ManufacturingFeature) {
  if (feature.kind === "hole") return 0x4f7cff;
  if (feature.kind === "pocket") return 0xf0a23b;
  if (feature.kind === "slot") return 0xa66be0;
  return 0x18b89a;
}

export function ModelViewer({ modelUrl, features, selectedFeatureIds, onSelectFeature, toolpathSegments = [], materialSnapshotUrls = [], materialSnapshotStages = [], initialToolpathSegments = [], profileBoundaries = [], simulation = null, simulationBlocked = false, turningStage = null, camoticsSurface = null, fixtureComponents = [], animateToolpath = false, initialProgress = 0, playbackResetToken = 0, operationTools = {}, topologyEdges = [], activeOperationId, isFinalOperation = false, toolpathLoaded = true, playbackMode = "cumulative", onPlaybackModeChange, formingPreview = null, spatialDefects = null, onSelectDefect, viewMode = "特征", workAxis = null, activeOperationLabel = "" }: Props) {
  const hostRef = useRef<HTMLDivElement>(null);
  const axisHostRef = useRef<HTMLDivElement>(null);
  const markersRef = useRef<Map<string, THREE.Mesh>>(new Map());
  const onSelectRef = useRef(onSelectFeature);
  const onSelectDefectRef = useRef(onSelectDefect);
  const selectedIdsRef = useRef(selectedFeatureIds);
  const playbackRef = useRef({ playing: false, progress: initialProgress, speed: 1 });
  const progressByModeRef = useRef<Record<string, number>>({});
  const lastPlaybackResetTokenRef = useRef(playbackResetToken);
  const cameraByModeRef = useRef<Record<string, { position: [number, number, number]; target: [number, number, number]; zoom: number }>>({});
  const playbackKey = `${modelUrl}:${activeOperationId ?? "none"}:${playbackMode}`;
  const cameraKey = `${playbackKey}:${viewMode}:${toolpathSegments.length}:${materialSnapshotUrls.length}`;
  const [playing, setPlaying] = useState(false);
  const [progress, setProgress] = useState(initialProgress);
  const [speed, setSpeed] = useState(1);
  const [snapshotLoadError, setSnapshotLoadError] = useState<string | null>(null);
  const [activeMotion, setActiveMotion] = useState<{ operation: string; setup?: string; motion: string; removesMaterial: boolean } | null>(null);
  const [targetVisible, setTargetVisible] = useState(false);
  const targetVisibleRef = useRef(false);
  const [toolVisible, setToolVisible] = useState(true);
  const [trailVisible, setTrailVisible] = useState(true);
  const [activeDefect, setActiveDefect] = useState<SpatialDefectRegion | null>(null);
  const viewApiRef = useRef<{
    setView: (view: "iso" | "top" | "front" | "fit") => void;
    setTargetVisible: (visible: boolean) => void;
    setToolVisible: (visible: boolean) => void;
    setTrailVisible: (visible: boolean) => void;
  } | null>(null);
  // Preview geometry arrives asynchronously after an operation is selected.
  // Keep the user's camera only while the visible machining content is the
  // same; otherwise the newly arrived stock/result must receive a fresh fit.
  const cameraContentKey = [
    activeOperationId ?? "",
    playbackMode,
    viewMode,
    toolpathSegments.length,
    materialSnapshotUrls.join(","),
    initialToolpathSegments.length,
    turningStage
      ? `${turningStage.operation_id}:${turningStage.before_samples.length}:${turningStage.after_samples.length}`
      : "no-turning-stage",
    simulation
      ? `${simulation.surface.columns}:${simulation.surface.rows}:${simulation.surface.heights.length}`
      : "no-simulation",
    camoticsSurface?.url ?? "no-camotics-surface",
    formingPreview?.stages.length ?? 0,
  ].join("|");

  const updatePlaying = (value: boolean) => {
    if (value && playbackRef.current.progress >= 0.999) {
      playbackRef.current.progress = 0;
      setProgress(0);
    }
    playbackRef.current.playing = value;
    setPlaying(value);
  };

  const updateProgress = (value: number) => {
    playbackRef.current.progress = value;
    playbackRef.current.playing = false;
    progressByModeRef.current[playbackKey] = value;
    setProgress(value);
    setPlaying(false);
  };

  const updateSpeed = (value: number) => {
    playbackRef.current.speed = value;
    setSpeed(value);
  };

  useEffect(() => {
    onSelectRef.current = onSelectFeature;
  }, [onSelectFeature]);

  useEffect(() => {
    onSelectDefectRef.current = onSelectDefect;
  }, [onSelectDefect]);

  useEffect(() => {
    if (lastPlaybackResetTokenRef.current !== playbackResetToken) {
      progressByModeRef.current[playbackKey] = 0;
      lastPlaybackResetTokenRef.current = playbackResetToken;
    }
    const savedProgress = progressByModeRef.current[playbackKey];
    playbackRef.current.playing = false;
    playbackRef.current.progress = savedProgress ?? 0;
    const reset = window.setTimeout(() => {
      setPlaying(playbackRef.current.playing);
      setProgress(playbackRef.current.progress);
      setActiveMotion(null);
      setSnapshotLoadError(null);
    }, 0);
    return () => window.clearTimeout(reset);
  }, [playbackKey, playbackResetToken, viewMode]);

  useEffect(() => {
    selectedIdsRef.current = selectedFeatureIds;
    for (const [featureId, marker] of markersRef.current) {
      const selected = selectedFeatureIds.includes(featureId);
      const material = marker.material as THREE.MeshBasicMaterial;
      material.color.set(viewMode === "工艺" ? 0x21c997 : Number(marker.userData.baseColor ?? 0x4f7cff));
      material.opacity = selected ? 0.68 : viewMode === "特征" ? 0.26 : 0.05;
      marker.renderOrder = selected ? 5 : 3;
    }
  }, [selectedFeatureIds, viewMode]);

  useEffect(() => {
    const host = hostRef.current;
    const axisHost = axisHostRef.current;
    if (!host || !axisHost) return;
    const cameraMemory = cameraByModeRef.current;
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(
      viewMode === "特征" ? 0xdce7f1
        : viewMode === "工艺" ? 0xe4e9ec
          : viewMode === "刀路" ? 0xd6e1e8
            : 0xc9cdd0,
    );
    const camera = new THREE.OrthographicCamera(-100, 100, 100, -100, 0.1, 100000);
    camera.up.set(0, 0, 1);
    camera.position.set(120, -140, 150);
    const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 0.92;
    // Shadows add substantial GPU work and can shimmer on dense CAD meshes
    // while orbiting. They are useful for simulation, but not for the normal
    // feature/process views where the environment lights already describe form.
    renderer.shadowMap.enabled = viewMode === "仿真";
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    host.appendChild(renderer.domElement);

    const axisRenderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    axisRenderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    axisRenderer.setSize(72, 72, false);
    axisRenderer.setClearColor(0x000000, 0);
    axisRenderer.domElement.setAttribute("aria-hidden", "true");
    axisHost.appendChild(axisRenderer.domElement);
    const axisScene = new THREE.Scene();
    const axisCamera = new THREE.OrthographicCamera(-1.4, 1.4, 1.4, -1.4, 0.1, 10);
    axisCamera.position.set(0, 0, 5);
    const axisGroup = new THREE.Group();
    const axisLength = 0.92;
    axisGroup.add(
      new THREE.ArrowHelper(new THREE.Vector3(1, 0, 0), new THREE.Vector3(), axisLength, 0xe34b4b, 0.2, 0.11),
      new THREE.ArrowHelper(new THREE.Vector3(0, 1, 0), new THREE.Vector3(), axisLength, 0x35a85b, 0.2, 0.11),
      new THREE.ArrowHelper(new THREE.Vector3(0, 0, 1), new THREE.Vector3(), axisLength, 0x3978e8, 0.2, 0.11),
    );
    const axisLabelResources: { sprite: THREE.Sprite; texture: THREE.CanvasTexture }[] = [];
    const addAxisLabel = (text: string, color: string, position: THREE.Vector3) => {
      const canvas = document.createElement("canvas");
      canvas.width = 64;
      canvas.height = 64;
      const context = canvas.getContext("2d");
      if (!context) return;
      context.font = "700 38px Arial";
      context.textAlign = "center";
      context.textBaseline = "middle";
      context.fillStyle = "#ffffff";
      context.lineWidth = 8;
      context.strokeStyle = "#ffffff";
      context.strokeText(text, 32, 34);
      context.fillStyle = color;
      context.fillText(text, 32, 34);
      const texture = new THREE.CanvasTexture(canvas);
      texture.colorSpace = THREE.SRGBColorSpace;
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, transparent: true, depthTest: false }));
      sprite.position.copy(position);
      sprite.scale.setScalar(0.38);
      axisGroup.add(sprite);
      axisLabelResources.push({ sprite, texture });
    };
    addAxisLabel("X", "#d93636", new THREE.Vector3(1.15, 0, 0));
    addAxisLabel("Y", "#218b46", new THREE.Vector3(0, 1.15, 0));
    addAxisLabel("Z", "#2868d7", new THREE.Vector3(0, 0, 1.15));
    axisGroup.add(new THREE.Mesh(
      new THREE.SphereGeometry(0.075, 16, 12),
      new THREE.MeshBasicMaterial({ color: 0x657287 }),
    ));
    axisScene.add(axisGroup);

    const pmremGenerator = new THREE.PMREMGenerator(renderer);
    const environmentTarget = pmremGenerator.fromScene(new RoomEnvironment(), 0.04);
    scene.environment = environmentTarget.texture;

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    scene.add(new THREE.HemisphereLight(0xf4f7f8, 0x59636a, 1.15));
    const light = new THREE.DirectionalLight(0xffffff, 2.15);
    light.position.set(140, -100, 220);
    light.castShadow = true;
    light.shadow.mapSize.set(2048, 2048);
    scene.add(light);
    const fillLight = new THREE.DirectionalLight(0xc5d8e0, 0.72);
    fillLight.position.set(-130, -70, 90);
    scene.add(fillLight);
    const rimLight = new THREE.DirectionalLight(0xffffff, 0.62);
    rimLight.position.set(40, 160, 50);
    scene.add(rimLight);
    const floorGrid = new THREE.GridHelper(240, 24, 0x8a949e, 0xaab1b8);
    floorGrid.rotation.x = Math.PI / 2;
    const floorMaterials = Array.isArray(floorGrid.material) ? floorGrid.material : [floorGrid.material];
    for (const material of floorMaterials) {
      material.transparent = true;
      material.opacity = 0.25;
      material.depthWrite = false;
    }
    scene.add(floorGrid);

    let model: THREE.Mesh | null = null;
    let lastFormingFactor = -1;
    let formingActuator: THREE.Mesh | null = null;
    let formingStock: THREE.Mesh | null = null;
    let formingLowerDie: THREE.Mesh | null = null;
    let formingCutOutline: THREE.LineLoop | null = null;
    let formingCutPaths: THREE.LineSegments | null = null;
    const formingCutSegmentPoints: THREE.Vector3[] = [];
    let simulationMesh: THREE.Mesh | null = null;
    let simulationLowerMesh: THREE.Mesh | null = null;
    let simulationWalls: THREE.Mesh | null = null;
    let turningBeforeMesh: THREE.Mesh | null = null;
    let turningAfterMesh: THREE.Mesh | null = null;
    let turningBeforeSamples: TurningStageView["before_samples"] = [];
    let turningAfterSamples: TurningStageView["after_samples"] = [];
    let camoticsMesh: THREE.Mesh | null = null;
    let playbackTool: THREE.Group | null = null;
    let playbackCutter: THREE.Mesh | null = null;
    let playbackDrillTip: THREE.Mesh | null = null;
    let playbackHolder: THREE.Mesh | null = null;
    let activePath: THREE.Line | null = null;
    let trailPath: THREE.LineSegments | null = null;
    let cadEdges: THREE.LineSegments | null = null;
    let modelCenter = new THREE.Vector3();
    let viewSize = 100;
    let viewHeight = 200;
    let viewAspect = 1;
    let dynamicHeights: Float32Array | null = null;
    let dynamicLowerHeights: Float32Array | null = null;
    let surfacePositions: THREE.BufferAttribute | null = null;
    let lowerSurfacePositions: THREE.BufferAttribute | null = null;
    let materialProgress = 0;
    let lastNormalsProgress = 0;
    let processedRatios: number[] = [];
    const outsideProfileIndices: number[] = [];
    let profileDetachWeight = Number.POSITIVE_INFINITY;
    const fixtureMeshes: { mesh: THREE.Mesh; setupId?: string | null }[] = [];
    const defectRegionMeshes: THREE.Mesh[] = [];
    const materialSnapshotMeshes: Array<THREE.Mesh | null> = Array(materialSnapshotUrls.length).fill(null);
    const loadingSnapshotIndexes = new Set<number>();
    const failedSnapshotIndexes = new Set<number>();
    let disposed = false;
    const requestMaterialSnapshot = (snapshotIndex: number) => {
      const snapshotUrl = materialSnapshotUrls[snapshotIndex];
      if (!model || !snapshotUrl || materialSnapshotMeshes[snapshotIndex] || loadingSnapshotIndexes.has(snapshotIndex) || failedSnapshotIndexes.has(snapshotIndex)) return;
      loadingSnapshotIndexes.add(snapshotIndex);
      new STLLoader().load(snapshotUrl, (snapshotGeometry) => {
        loadingSnapshotIndexes.delete(snapshotIndex);
        if (disposed) {
          snapshotGeometry.dispose();
          return;
        }
        snapshotGeometry.translate(-modelCenter.x, -modelCenter.y, -modelCenter.z);
        const renderSnapshot = toCreasedNormals(snapshotGeometry, THREE.MathUtils.degToRad(52));
        if (renderSnapshot !== snapshotGeometry) snapshotGeometry.dispose();
        const snapshotMesh = new THREE.Mesh(
          renderSnapshot,
          new THREE.MeshPhysicalMaterial({
            color: 0x718083,
            roughness: 0.5,
            metalness: 0.08,
            clearcoat: 0.04,
            clearcoatRoughness: 0.68,
          }),
        );
        snapshotMesh.castShadow = true;
        snapshotMesh.receiveShadow = true;
        snapshotMesh.visible = false;
        materialSnapshotMeshes[snapshotIndex] = snapshotMesh;
        scene.add(snapshotMesh);
      }, undefined, () => {
        loadingSnapshotIndexes.delete(snapshotIndex);
        if (!disposed) {
          failedSnapshotIndexes.add(snapshotIndex);
          playbackRef.current.playing = false;
          setPlaying(false);
          setSnapshotLoadError(`实体材料第 ${snapshotIndex + 1} 帧加载失败，请检查服务后重新打开工序。`);
        }
      });
    };
    let defectPointCloud: THREE.Points | null = null;
    let animation = 0;
    let lastFrameTime = performance.now();
    let lastReportedSegment = -1;
    const markerGroup = new THREE.Group();
    scene.add(markerGroup);
    markersRef.current = new Map();
    const surfaceFrame = simulation?.surface.frame ?? {
      x: { x: 1, y: 0, z: 0 },
      y: { x: 0, y: 1, z: 0 },
      z: { x: 0, y: 0, z: 1 },
    };
    const localToScene = (x: number, y: number, z: number) => new THREE.Vector3(
      x * surfaceFrame.x.x + y * surfaceFrame.y.x + z * surfaceFrame.z.x - modelCenter.x,
      x * surfaceFrame.x.y + y * surfaceFrame.y.y + z * surfaceFrame.z.y - modelCenter.y,
      x * surfaceFrame.x.z + y * surfaceFrame.y.z + z * surfaceFrame.z.z - modelCenter.z,
    );
    const worldToLocal = (x: number, y: number, z: number) => ({
      x: x * surfaceFrame.x.x + y * surfaceFrame.x.y + z * surfaceFrame.x.z,
      y: x * surfaceFrame.y.x + y * surfaceFrame.y.y + z * surfaceFrame.y.z,
      z: x * surfaceFrame.z.x + y * surfaceFrame.z.y + z * surfaceFrame.z.z,
    });
    const resize = () => {
      const width = host.clientWidth;
      const height = host.clientHeight;
      renderer.setSize(width, height, false);
      viewAspect = width / Math.max(height, 1);
      camera.left = -viewHeight * viewAspect / 2;
      camera.right = viewHeight * viewAspect / 2;
      camera.top = viewHeight / 2;
      camera.bottom = -viewHeight / 2;
      camera.updateProjectionMatrix();
    };
    const observer = new ResizeObserver(resize);
    observer.observe(host);
    resize();

    new STLLoader().load(modelUrl, (geometry) => {
      geometry.computeBoundingBox();
      const bounds = geometry.boundingBox;
      modelCenter = new THREE.Vector3();
      if (bounds) {
        bounds.getCenter(modelCenter);
        geometry.translate(-modelCenter.x, -modelCenter.y, -modelCenter.z);
        const size = bounds.getSize(new THREE.Vector3()).length();
        viewSize = size;
        camera.position.set(size * 1.25, -size * 1.7, size * 2.0);
        camera.near = Math.max(size / 1000, 0.01);
        camera.far = size * 100;
        camera.updateProjectionMatrix();
        const centeredMinimumZ = bounds.min.z - modelCenter.z;
        floorGrid.position.z = centeredMinimumZ - Math.max(size * 0.025, 0.5);
        floorGrid.scale.setScalar(Math.max(size / 170, 0.7));
      }
      const renderGeometry = toCreasedNormals(geometry, THREE.MathUtils.degToRad(52));
      const modelPositions = renderGeometry.getAttribute("position") as THREE.BufferAttribute;
      model = new THREE.Mesh(
        renderGeometry,
        new THREE.MeshPhysicalMaterial({
          color: simulation || turningStage ? UG_TARGET_COLOR : viewMode === "特征" ? 0x768793 : viewMode === "刀路" ? 0x87969d : UG_PART_COLOR,
          roughness: 0.56,
          metalness: 0.06,
          clearcoat: 0.04,
          clearcoatRoughness: 0.68,
          envMapIntensity: 0.5,
          // Keep the normal CAD view opaque. Transparent whole-model rendering
          // makes triangle ordering and coplanar edge overlays visibly shimmer
          // as the camera moves.
          transparent: Boolean(simulation || turningStage || formingPreview || viewMode === "刀路"),
          opacity: simulation || turningStage ? 0.16 : viewMode === "刀路" ? 0.34 : 1,
          depthWrite: !simulation && !turningStage && viewMode !== "刀路",
          // Keep coplanar CAD edge overlays stable when zoomed in. Without a
          // small depth bias the edge and surface alternate at sub-pixel depth,
          // producing the broken/dotted outlines visible at high zoom.
          polygonOffset: true,
          polygonOffsetFactor: simulation || turningStage ? -2 : 2,
          polygonOffsetUnits: simulation || turningStage ? -2 : 2,
        }),
      );
      // Keep the target optional during simulation. The target and final IPW
      // are often nearly coplanar, so drawing both produces white shimmer that
      // looks like a rough machined surface even when the height field is clean.
      model.visible = materialSnapshotUrls.length === 0 && (!(simulation || turningStage) || targetVisibleRef.current);
      model.castShadow = true;
      model.receiveShadow = true;
      scene.add(model);

      requestMaterialSnapshot(0);

      if (turningStage) {
        const axisOrigin = new THREE.Vector3(
          turningStage.axis_origin.x,
          turningStage.axis_origin.y,
          turningStage.axis_origin.z,
        );
        const axisDirection = new THREE.Vector3(
          turningStage.axis_direction.x,
          turningStage.axis_direction.y,
          turningStage.axis_direction.z,
        ).normalize();
        const makeStockMesh = (
          samples: TurningStageView["before_samples"],
          color: number,
          opacity: number,
        ) => {
          const ordered = [...samples].sort((left, right) => left.z - right.z);
          const profile = [
            ...ordered.map((sample) => new THREE.Vector2(Math.max(sample.outer_radius, 0), sample.z)),
            ...ordered.slice().reverse().map((sample) => new THREE.Vector2(Math.max(sample.inner_radius, 0), sample.z)),
          ];
          const stockGeometry = new THREE.LatheGeometry(profile, 64);
          stockGeometry.computeVertexNormals();
          const material = new THREE.MeshPhysicalMaterial({
            color,
            roughness: 0.38,
            metalness: 0.42,
            clearcoat: 0.12,
            clearcoatRoughness: 0.5,
            transparent: true,
            opacity,
            side: THREE.DoubleSide,
            depthWrite: opacity > 0.75,
          });
          const mesh = new THREE.Mesh(stockGeometry, material);
          mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), axisDirection);
          mesh.position.copy(axisOrigin).sub(modelCenter);
          mesh.castShadow = true;
          mesh.receiveShadow = true;
          return mesh;
        };
        turningBeforeMesh = makeStockMesh(turningStage.before_samples, 0x7f8c92, 0.78);
        turningAfterMesh = makeStockMesh(turningStage.after_samples, 0x268b7a, 0.28);
        turningBeforeSamples = [...turningStage.before_samples].sort((left, right) => left.z - right.z);
        turningAfterSamples = [...turningStage.after_samples].sort((left, right) => left.z - right.z);
        turningBeforeMesh.renderOrder = 3;
        turningAfterMesh.renderOrder = 4;
        scene.add(turningBeforeMesh, turningAfterMesh);
      }

      if (formingPreview) {
        const partSize = bounds?.getSize(new THREE.Vector3()) ?? new THREE.Vector3(viewSize, viewSize * 0.1, viewSize * 0.6);
        const sheetThickness = Math.max(formingPreview.nominal_thickness_mm, viewSize * 0.004);
        formingStock = new THREE.Mesh(
          new THREE.BoxGeometry(partSize.x * 1.14, sheetThickness, partSize.z * 1.14),
          new THREE.MeshPhysicalMaterial({
            color: 0xaab5b9, roughness: 0.46, metalness: 0.28,
            transparent: true, opacity: 0.92, depthWrite: true,
          }),
        );
        formingStock.castShadow = true;
        formingStock.receiveShadow = true;
        scene.add(formingStock);

        const cutHalfX = partSize.x * 0.535;
        const cutHalfZ = partSize.z * 0.535;
        formingCutOutline = new THREE.LineLoop(
          new THREE.BufferGeometry().setFromPoints([
            new THREE.Vector3(-cutHalfX, -sheetThickness * 0.75, -cutHalfZ),
            new THREE.Vector3(cutHalfX, -sheetThickness * 0.75, -cutHalfZ),
            new THREE.Vector3(cutHalfX, -sheetThickness * 0.75, cutHalfZ),
            new THREE.Vector3(-cutHalfX, -sheetThickness * 0.75, cutHalfZ),
          ]),
          new THREE.LineBasicMaterial({ color: 0x66dfff, transparent: true, opacity: 0.95, depthTest: false }),
        );
        formingCutOutline.visible = false;
        formingCutOutline.renderOrder = 8;
        scene.add(formingCutOutline);

        const uniqueProjectedSegments = new Set<string>();
        for (const edge of topologyEdges) {
          for (let index = 1; index < edge.length; index += 1) {
            const start = new THREE.Vector3(
              edge[index - 1].x - modelCenter.x,
              -sheetThickness * 1.15,
              edge[index - 1].z - modelCenter.z,
            );
            const end = new THREE.Vector3(
              edge[index].x - modelCenter.x,
              -sheetThickness * 1.15,
              edge[index].z - modelCenter.z,
            );
            if (start.distanceToSquared(end) < 1e-8) continue;
            const pointKey = (point: THREE.Vector3) => `${Math.round(point.x * 50)},${Math.round(point.z * 50)}`;
            const firstKey = pointKey(start);
            const secondKey = pointKey(end);
            const segmentKey = firstKey < secondKey ? `${firstKey}|${secondKey}` : `${secondKey}|${firstKey}`;
            if (uniqueProjectedSegments.has(segmentKey)) continue;
            uniqueProjectedSegments.add(segmentKey);
            formingCutSegmentPoints.push(start, end);
          }
        }
        if (formingCutSegmentPoints.length) {
          formingCutPaths = new THREE.LineSegments(
            new THREE.BufferGeometry().setFromPoints(formingCutSegmentPoints),
            new THREE.LineBasicMaterial({
              color: 0x5fffd0, transparent: true, opacity: 0.94,
              depthTest: false, depthWrite: false,
            }),
          );
          formingCutPaths.geometry.setDrawRange(0, 0);
          formingCutPaths.visible = false;
          formingCutPaths.renderOrder = 9;
          scene.add(formingCutPaths);
        }

        formingLowerDie = new THREE.Mesh(
          renderGeometry.clone(),
          new THREE.MeshPhysicalMaterial({
            color: 0x31547b, roughness: 0.34, metalness: 0.48,
            transparent: true, opacity: 0.13, depthWrite: false, side: THREE.DoubleSide,
          }),
        );
        formingLowerDie.position.y = viewSize * 0.14;
        formingLowerDie.visible = false;
        formingLowerDie.renderOrder = 1;
        scene.add(formingLowerDie);

        formingActuator = new THREE.Mesh(
          new THREE.CylinderGeometry(0.5, 0.8, 1, 20),
          new THREE.MeshPhysicalMaterial({
            color: 0x55b8d0, transparent: true, opacity: 0.28,
            roughness: 0.35, metalness: 0.18, depthWrite: false,
          }),
        );
        formingActuator.renderOrder = 6;
        scene.add(formingActuator);
      }

      const edgeCoordinates: number[] = [];
      if (topologyEdges.length) {
        for (const edge of topologyEdges) {
          for (let index = 1; index < edge.length; index += 1) {
            const start = edge[index - 1];
            const end = edge[index];
            edgeCoordinates.push(
              start.x - modelCenter.x, start.y - modelCenter.y, start.z - modelCenter.z,
              end.x - modelCenter.x, end.y - modelCenter.y, end.z - modelCenter.z,
            );
          }
        }
      }
      const edgeGeometry = edgeCoordinates.length
        ? new THREE.BufferGeometry().setAttribute("position", new THREE.Float32BufferAttribute(edgeCoordinates, 3))
        : new THREE.EdgesGeometry(renderGeometry, 32);
      cadEdges = new THREE.LineSegments(
        edgeGeometry,
        new THREE.LineBasicMaterial({
          color: UG_EDGE_COLOR,
          transparent: true,
          opacity: simulation || turningStage ? 0.24 : viewMode === "刀路" ? 0.2 : viewMode === "特征" ? 0.38 : 0.64,
          depthTest: true,
          depthWrite: false,
        }),
      );
      cadEdges.renderOrder = 2;
      cadEdges.visible = materialSnapshotUrls.length === 0 && !formingPreview && (!(simulation || turningStage) || targetVisibleRef.current);
      scene.add(cadEdges);
      if (renderGeometry !== geometry) geometry.dispose();

      if (camoticsSurface) {
        new STLLoader().load(camoticsSurface.url, (stockGeometry) => {
          const frame = camoticsSurface.frame;
          const transform = new THREE.Matrix4().set(
            frame.x.x, frame.y.x, frame.z.x, -modelCenter.x,
            frame.x.y, frame.y.y, frame.z.y, -modelCenter.y,
            frame.x.z, frame.y.z, frame.z.z, -modelCenter.z,
            0, 0, 0, 1,
          );
          stockGeometry.applyMatrix4(transform);
          stockGeometry.computeVertexNormals();
          camoticsMesh = new THREE.Mesh(
            toCreasedNormals(stockGeometry, THREE.MathUtils.degToRad(42)),
            new THREE.MeshPhysicalMaterial({
              color: 0xaebbc1,
              roughness: 0.32,
              metalness: 0.62,
              clearcoat: 0.18,
              clearcoatRoughness: 0.45,
              envMapIntensity: 0.9,
            }),
          );
          camoticsMesh.visible = !animateToolpath || playbackRef.current.progress >= 0.999;
          camoticsMesh.castShadow = true;
          camoticsMesh.receiveShadow = true;
          scene.add(camoticsMesh);
        });
      }

      if (animateToolpath && toolpathSegments.length) {
        playbackTool = new THREE.Group();
        playbackCutter = new THREE.Mesh(
          new THREE.CylinderGeometry(0.5, 0.5, 1, 20),
          new THREE.MeshStandardMaterial({ color: 0xc7d1d7, emissive: 0x172129, metalness: 0.82, roughness: 0.18 }),
        );
        playbackDrillTip = new THREE.Mesh(
          new THREE.ConeGeometry(0.5, 1, 20),
          new THREE.MeshStandardMaterial({ color: 0xc7d1d7, emissive: 0x172129, metalness: 0.82, roughness: 0.18 }),
        );
        playbackDrillTip.rotation.x = Math.PI;
        playbackHolder = new THREE.Mesh(
          new THREE.CylinderGeometry(0.38, 0.5, 1, 32),
          new THREE.MeshStandardMaterial({ color: 0x4e5961, metalness: 0.76, roughness: 0.24, transparent: true, opacity: 0.55, depthWrite: false }),
        );
        playbackTool.add(playbackCutter, playbackDrillTip, playbackHolder);
        playbackTool.renderOrder = 12;
        scene.add(playbackTool);
        const activeGeometry = new THREE.BufferGeometry();
        activeGeometry.setAttribute("position", new THREE.Float32BufferAttribute([0, 0, 0, 0, 0, 0], 3));
        activePath = new THREE.Line(activeGeometry, new THREE.LineBasicMaterial({ color: 0xffffff, depthTest: false }));
        activePath.renderOrder = 11;
        scene.add(activePath);
        const trailCoordinates = toolpathSegments.flatMap((segment) => [
          segment.x1 - modelCenter.x, segment.y1 - modelCenter.y, segment.z1 - modelCenter.z,
          segment.x2 - modelCenter.x, segment.y2 - modelCenter.y, segment.z2 - modelCenter.z,
        ]);
        const trailColors = toolpathSegments.flatMap((segment) => {
          const color = new THREE.Color(segment.motion === "cut" ? 0x20c997 : 0x55a9e0);
          return [color.r, color.g, color.b, color.r, color.g, color.b];
        });
        const trailGeometry = new THREE.BufferGeometry();
        trailGeometry.setAttribute("position", new THREE.Float32BufferAttribute(trailCoordinates, 3));
        trailGeometry.setAttribute("color", new THREE.Float32BufferAttribute(trailColors, 3));
        trailGeometry.setDrawRange(0, 0);
        trailPath = new THREE.LineSegments(
          trailGeometry,
          new THREE.LineBasicMaterial({ vertexColors: true, transparent: true, opacity: 0.34, depthTest: false }),
        );
        trailPath.renderOrder = 10;
        trailPath.visible = true;
        scene.add(trailPath);

      }

      for (const fixture of fixtureComponents) {
        const size = fixture.bounds.size;
        const fixtureGeometry = new THREE.BoxGeometry(size.x, size.y, size.z);
        const sacrificial = fixture.kind === "sacrificial";
        const fixtureColor = sacrificial ? 0x4da98c : fixture.kind === "machine" ? 0xe15d65 : 0xe15d65;
        const fixtureMesh = new THREE.Mesh(
          fixtureGeometry,
          new THREE.MeshBasicMaterial({
            color: fixtureColor,
            transparent: true,
            opacity: sacrificial ? 0.08 : 0.12,
            wireframe: !sacrificial,
            depthTest: true,
          }),
        );
        fixtureMesh.position.set(
          (fixture.bounds.minimum.x + fixture.bounds.maximum.x) / 2 - modelCenter.x,
          (fixture.bounds.minimum.y + fixture.bounds.maximum.y) / 2 - modelCenter.y,
          (fixture.bounds.minimum.z + fixture.bounds.maximum.z) / 2 - modelCenter.z,
        );
        fixtureMesh.renderOrder = 6;
        markerGroup.add(fixtureMesh);
        fixtureMeshes.push({ mesh: fixtureMesh, setupId: fixture.setup_id });
      }

      if (simulation && !camoticsSurface) {
        const surface = simulation.surface;
        const positions: number[] = [];
        const lowerPositions: number[] = [];
        const indices: number[] = [];
        const finalLowerHeights = surface.lower_heights
          ?? new Array(surface.columns * surface.rows).fill(surface.bottom_z);
        for (let row = 0; row < surface.rows; row += 1) {
          for (let column = 0; column < surface.columns; column += 1) {
            const index = row * surface.columns + column;
            const x = surface.origin.x + column * surface.resolution_mm;
            const y = surface.origin.y + row * surface.resolution_mm;
            const point = localToScene(
              x,
              y,
              animateToolpath ? surface.top_z : surface.heights[index],
            );
            const lowerPoint = localToScene(
              x,
              y,
              animateToolpath ? surface.bottom_z : finalLowerHeights[index],
            );
            positions.push(point.x, point.y, point.z);
            lowerPositions.push(lowerPoint.x, lowerPoint.y, lowerPoint.z);
          }
        }
        for (let row = 0; row < surface.rows - 1; row += 1) {
          for (let column = 0; column < surface.columns - 1; column += 1) {
            const current = row * surface.columns + column;
            const nextRow = current + surface.columns;
            indices.push(current, current + 1, nextRow, current + 1, nextRow + 1, nextRow);
          }
        }
        const surfaceGeometry = new THREE.BufferGeometry();
        surfaceGeometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
        surfacePositions = surfaceGeometry.getAttribute("position") as THREE.BufferAttribute;
        surfaceGeometry.setIndex(indices);
        surfaceGeometry.computeVertexNormals();
        const lowerGeometry = new THREE.BufferGeometry();
        lowerGeometry.setAttribute("position", new THREE.Float32BufferAttribute(lowerPositions, 3));
        lowerSurfacePositions = lowerGeometry.getAttribute("position") as THREE.BufferAttribute;
        lowerGeometry.setIndex(indices);
        lowerGeometry.computeVertexNormals();
        dynamicHeights = new Float32Array(surface.columns * surface.rows);
        dynamicLowerHeights = new Float32Array(surface.columns * surface.rows);
        dynamicHeights.fill(animateToolpath ? surface.top_z : 0);
        dynamicLowerHeights.fill(surface.bottom_z);
        if (!animateToolpath) dynamicHeights.set(surface.heights);
        if (!animateToolpath) dynamicLowerHeights.set(finalLowerHeights);
        processedRatios = new Array(toolpathSegments.length).fill(0);
        simulationMesh = new THREE.Mesh(
          surfaceGeometry,
          new THREE.MeshStandardMaterial({ color: 0xbfc8cd, roughness: 0.68, metalness: 0.18, side: THREE.DoubleSide }),
        );
        simulationMesh.renderOrder = 4;
        scene.add(simulationMesh);
        simulationLowerMesh = new THREE.Mesh(
          lowerGeometry,
          new THREE.MeshStandardMaterial({ color: 0xa9b5bb, roughness: 0.72, metalness: 0.14, side: THREE.DoubleSide }),
        );
        simulationLowerMesh.renderOrder = 4;
        scene.add(simulationLowerMesh);
        simulationWalls = new THREE.Mesh(
          new THREE.BufferGeometry(),
          new THREE.MeshStandardMaterial({ color: 0x8f9ca4, roughness: 0.74, metalness: 0.12, side: THREE.DoubleSide }),
        );
        simulationWalls.renderOrder = 5;
        scene.add(simulationWalls);
        rebuildMaterialWalls();
      }

      for (const motion of (animateToolpath ? [] : ["rapid", "cut"]) as ("rapid" | "cut")[]) {
        const coordinates: number[] = [];
        for (const segment of toolpathSegments.filter((item) => item.motion === motion)) {
          coordinates.push(
            segment.x1 - modelCenter.x, segment.y1 - modelCenter.y, segment.z1 - modelCenter.z,
            segment.x2 - modelCenter.x, segment.y2 - modelCenter.y, segment.z2 - modelCenter.z,
          );
        }
        if (!coordinates.length) continue;
        const pathGeometry = new THREE.BufferGeometry();
        pathGeometry.setAttribute("position", new THREE.Float32BufferAttribute(coordinates, 3));
        const pathLines = new THREE.LineSegments(
          pathGeometry,
          new THREE.LineBasicMaterial({
            color: motion === "cut" ? 0x52d8af : 0x718397,
            transparent: motion === "rapid",
            opacity: motion === "rapid" ? 0.35 : 1,
            depthTest: false,
          }),
        );
        pathLines.renderOrder = 8;
        markerGroup.add(pathLines);
      }

      if (simulation && spatialDefects?.regions.length) {
        const pointPositions = spatialDefects.samples.flatMap((sample) => [
          sample.position.x - modelCenter.x,
          sample.position.y - modelCenter.y,
          sample.position.z - modelCenter.z,
        ]);
        const pointColors = spatialDefects.samples.flatMap((sample) => {
          const color = new THREE.Color(sample.kind === "overcut" ? 0xff3b30 : 0xffb020);
          return [color.r, color.g, color.b];
        });
        if (pointPositions.length) {
          const pointGeometry = new THREE.BufferGeometry();
          pointGeometry.setAttribute("position", new THREE.Float32BufferAttribute(pointPositions, 3));
          pointGeometry.setAttribute("color", new THREE.Float32BufferAttribute(pointColors, 3));
          defectPointCloud = new THREE.Points(
            pointGeometry,
            new THREE.PointsMaterial({
              size: Math.max(viewSize * 0.018, 1.5), vertexColors: true,
              transparent: true, opacity: 0.82, depthTest: false, sizeAttenuation: true,
            }),
          );
          defectPointCloud.renderOrder = 18;
          markerGroup.add(defectPointCloud);
        }
        for (const region of spatialDefects.regions) {
          const size = new THREE.Vector3(
            Math.max(region.bounds.maximum.x - region.bounds.minimum.x, viewSize * 0.008),
            Math.max(region.bounds.maximum.y - region.bounds.minimum.y, viewSize * 0.008),
            Math.max(region.bounds.maximum.z - region.bounds.minimum.z, viewSize * 0.008),
          );
          const regionMesh = new THREE.Mesh(
            new THREE.BoxGeometry(size.x, size.y, size.z),
            new THREE.MeshBasicMaterial({
              color: region.kind === "overcut" ? 0xff3b30 : 0xffb020,
              transparent: true, opacity: 0.2, wireframe: true, depthTest: false,
            }),
          );
          regionMesh.position.set(
            (region.bounds.minimum.x + region.bounds.maximum.x) / 2 - modelCenter.x,
            (region.bounds.minimum.y + region.bounds.maximum.y) / 2 - modelCenter.y,
            (region.bounds.minimum.z + region.bounds.maximum.z) / 2 - modelCenter.z,
          );
          regionMesh.userData.defectRegion = region;
          regionMesh.renderOrder = 19;
          markerGroup.add(regionMesh);
          defectRegionMeshes.push(regionMesh);
        }
      }

      for (const feature of simulation ? [] : features) {
        let markerGeometry: THREE.BufferGeometry;
        let axisValue;
        let offset = 0;
        let cylinderMarker = false;
        if ("depth" in feature) {
          markerGeometry = new THREE.BoxGeometry(
            Math.max(feature.bounds.size.x || (Math.abs(feature.access_direction.x) * feature.depth), 0.2),
            Math.max(feature.bounds.size.y || (Math.abs(feature.access_direction.y) * feature.depth), 0.2),
            Math.max(feature.bounds.size.z || (Math.abs(feature.access_direction.z) * feature.depth), 0.2),
          );
          axisValue = feature.access_direction;
          offset = feature.depth / 2;
        } else {
          markerGeometry = new THREE.CylinderGeometry(
            Math.max(feature.radius * 1.04, 0.15),
            Math.max(feature.radius * 1.04, 0.15),
            Math.max(feature.length, 0.2),
            18,
            1,
            true,
          );
          axisValue = feature.axis;
          cylinderMarker = true;
        }
        const selected = selectedIdsRef.current.includes(feature.id);
        const baseColor = viewMode === "工艺" ? 0x21c997 : featureMarkerColor(feature);
        const marker = new THREE.Mesh(
          markerGeometry,
          new THREE.MeshBasicMaterial({
            color: baseColor,
            wireframe: true,
            transparent: true,
            opacity: selected ? 0.68 : viewMode === "特征" ? 0.26 : 0.05,
            depthTest: false,
          }),
        );
        const axis = new THREE.Vector3(axisValue.x, axisValue.y, axisValue.z).normalize();
        marker.position.set(
          feature.center.x + axis.x * offset - modelCenter.x,
          feature.center.y + axis.y * offset - modelCenter.y,
          feature.center.z + axis.z * offset - modelCenter.z,
        );
        if (cylinderMarker) marker.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), axis);
        marker.userData.featureId = feature.id;
        marker.userData.baseColor = baseColor;
        marker.renderOrder = selected ? 5 : 3;
        markerGroup.add(marker);
        markersRef.current.set(feature.id, marker);
      }

      controls.target.set(0, 0, 0);
      controls.update();
      const fitDirection = (directionValue: THREE.Vector3) => {
        const direction = directionValue.lengthSq() < 1e-6
          ? new THREE.Vector3(1.05, -1.35, 1.15).normalize()
          : directionValue.normalize();
        const forward = direction.clone().negate();
        const right = new THREE.Vector3().crossVectors(forward, camera.up);
        if (right.lengthSq() < 1e-6) right.set(1, 0, 0);
        right.normalize();
        const screenUp = new THREE.Vector3().crossVectors(right, forward).normalize();
        let minX = Number.POSITIVE_INFINITY;
        let maxX = Number.NEGATIVE_INFINITY;
        let minY = Number.POSITIVE_INFINITY;
        let maxY = Number.NEGATIVE_INFINITY;
        const positions = modelPositions.array as ArrayLike<number>;
        for (let index = 0; index < positions.length; index += 3) {
          const x = positions[index];
          const y = positions[index + 1];
          const z = positions[index + 2];
          const projectedX = x * right.x + y * right.y + z * right.z;
          const projectedY = x * screenUp.x + y * screenUp.y + z * screenUp.z;
          minX = Math.min(minX, projectedX);
          maxX = Math.max(maxX, projectedX);
          minY = Math.min(minY, projectedY);
          maxY = Math.max(maxY, projectedY);
        }
        // A machining preview can be larger than the finished CAD model (for
        // example, OP10 includes the full bar stock). Include all workpiece
        // result surfaces in the fit, but intentionally exclude tools,
        // fixtures and rapid paths so they cannot make the part unreadably
        // small.
        const fitSurfaces = [
          formingStock,
          camoticsMesh,
          simulationMesh,
          simulationLowerMesh,
          simulationWalls,
          turningBeforeMesh,
          turningAfterMesh,
        ].filter((surface): surface is THREE.Mesh => Boolean(surface));
        const boundsCorners = Array.from({ length: 8 }, () => new THREE.Vector3());
        for (const surface of fitSurfaces) {
          surface.updateMatrixWorld(true);
          const surfaceBounds = new THREE.Box3().setFromObject(surface);
          if (surfaceBounds.isEmpty()) continue;
          const { min, max } = surfaceBounds;
          boundsCorners[0].set(min.x, min.y, min.z);
          boundsCorners[1].set(min.x, min.y, max.z);
          boundsCorners[2].set(min.x, max.y, min.z);
          boundsCorners[3].set(min.x, max.y, max.z);
          boundsCorners[4].set(max.x, min.y, min.z);
          boundsCorners[5].set(max.x, min.y, max.z);
          boundsCorners[6].set(max.x, max.y, min.z);
          boundsCorners[7].set(max.x, max.y, max.z);
          for (const corner of boundsCorners) {
            const projectedX = corner.dot(right);
            const projectedY = corner.dot(screenUp);
            minX = Math.min(minX, projectedX);
            maxX = Math.max(maxX, projectedX);
            minY = Math.min(minY, projectedY);
            maxY = Math.max(maxY, projectedY);
          }
        }
        const halfWidth = (maxX - minX) / 2;
        const halfHeight = (maxY - minY) / 2;
        // Leave enough breathing room on first display. A tighter fit makes
        // large or elongated parts look cropped even when they technically fit.
        // Operation details and inspection cards float over the viewport. Keep
        // the whole part comfortably visible instead of fitting it edge-to-edge.
        viewHeight = Math.max(halfHeight * 2, halfWidth * 2 / Math.max(viewAspect, 0.1)) / 0.42;
        const radius = viewSize / 2;
        const distance = viewSize * 2.2;
        // Center the *visible projected bounds*, not the world origin. Turning
        // stock and cumulative IPW geometry can extend mostly to one side of
        // the CAD origin, which otherwise leaves the part in a viewport corner.
        const projectedCenterX = (minX + maxX) / 2;
        const projectedCenterY = (minY + maxY) / 2;
        const target = right.clone().multiplyScalar(projectedCenterX)
          .add(screenUp.clone().multiplyScalar(projectedCenterY));
        // Product layout reserves visual weight in the upper-left and places
        // controls along the bottom/right. Bias the camera target toward the
        // lower-right so the workpiece itself appears left and up on screen.
        // This is a screen-space composition offset and does not move geometry.
        target.add(right.clone().multiplyScalar(viewHeight * viewAspect * 0.07));
        target.add(screenUp.clone().multiplyScalar(-viewHeight * 0.07));
        controls.target.copy(target);
        camera.position.copy(target).add(direction.multiplyScalar(distance));
        camera.zoom = 1;
        camera.left = -viewHeight * viewAspect / 2;
        camera.right = viewHeight * viewAspect / 2;
        camera.top = viewHeight / 2;
        camera.bottom = -viewHeight / 2;
        camera.near = Math.max(distance - radius * 2, 0.01);
        camera.far = distance + radius * 4;
        camera.updateProjectionMatrix();
        controls.update();
      };
      const setView = (view: "iso" | "top" | "front" | "fit") => {
        const direction = view === "fit"
          ? camera.position.clone().sub(controls.target)
          : view === "top"
          ? new THREE.Vector3(0, 0, 1)
          : view === "front"
            ? new THREE.Vector3(0, -1, 0.12)
            : new THREE.Vector3(1.05, -1.35, 1.15);
        fitDirection(direction);
      };
      viewApiRef.current = {
        setView,
        setTargetVisible: (visible) => {
          if (model) model.visible = materialSnapshotUrls.length === 0 && (!(simulation || turningStage) || visible);
          if (cadEdges) cadEdges.visible = !(simulation || turningStage) || visible;
        },
        setToolVisible: (visible) => { if (playbackTool) playbackTool.visible = visible; },
        setTrailVisible: (visible) => { if (trailPath) trailPath.visible = visible; },
      };
      fitDirection(camera.position.clone());
      // Restore only the same mode, operation and geometry revision. New
      // previews receive a fresh fit so stale panning cannot hide the stock.
      const savedCamera = cameraMemory[cameraKey];
      if (savedCamera) {
        camera.position.fromArray(savedCamera.position);
        controls.target.fromArray(savedCamera.target);
        camera.zoom = savedCamera.zoom;
        camera.updateProjectionMatrix();
        controls.update();
      }
    });

    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    let pointerDownPosition: { x: number; y: number } | null = null;
    const setPointerFromEvent = (event: PointerEvent) => {
      const rectangle = renderer.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - rectangle.left) / rectangle.width) * 2 - 1;
      pointer.y = -((event.clientY - rectangle.top) / rectangle.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
    };
    const recordPointerDown = (event: PointerEvent) => {
      pointerDownPosition = { x: event.clientX, y: event.clientY };
    };
    const pickFeature = (event: PointerEvent) => {
      if (event.button !== 0 || !pointerDownPosition) return;
      const pointerTravel = Math.hypot(event.clientX - pointerDownPosition.x, event.clientY - pointerDownPosition.y);
      pointerDownPosition = null;
      if (pointerTravel > 4) return;
      setPointerFromEvent(event);
      const defectHit = raycaster.intersectObjects(defectRegionMeshes, false)[0];
      const defectRegion = defectHit?.object.userData.defectRegion as SpatialDefectRegion | undefined;
      if (defectRegion) {
        setActiveDefect(defectRegion);
        onSelectDefectRef.current?.(defectRegion);
        for (const mesh of defectRegionMeshes) {
          const material = mesh.material as THREE.MeshBasicMaterial;
          material.opacity = mesh.userData.defectRegion.id === defectRegion.id ? 0.58 : 0.12;
        }
        return;
      }
      const hit = raycaster.intersectObjects(Array.from(markersRef.current.values()), false)[0];
      const featureId = hit?.object.userData.featureId as string | undefined;
      if (featureId) onSelectRef.current(featureId);
    };
    renderer.domElement.addEventListener("pointerdown", recordPointerDown, true);
    renderer.domElement.addEventListener("pointerup", pickFeature);

    const segmentWeights = toolpathSegments.map((segment) => {
      const distance = Math.hypot(segment.x2 - segment.x1, segment.y2 - segment.y1, segment.z2 - segment.z1);
      return Math.max(0.01, distance / (segment.motion === "rapid" ? 4 : 1));
    });
    const totalWeight = segmentWeights.reduce((sum, value) => sum + value, 0) || 1;
    const cumulativeWeights = [0];
    for (const value of segmentWeights) cumulativeWeights.push(cumulativeWeights[cumulativeWeights.length - 1] + value);
    const stageWeights = materialSnapshotStages.map((stage) => {
      const first = toolpathSegments.findIndex((segment) => segment.operation_id === stage.operationId);
      let last = -1;
      for (let index = first; index < toolpathSegments.length; index += 1) {
        if (toolpathSegments[index]?.operation_id === stage.operationId) last = index;
      }
      return { ...stage, from: first < 0 ? 0 : cumulativeWeights[first], to: last < 0 ? 0 : cumulativeWeights[last + 1] };
    });
    const snapshotIndexForProgress = (currentProgress: number) => {
      const targetWeight = currentProgress * totalWeight;
      const currentStage = stageWeights.find((stage) => targetWeight <= stage.to && stage.to > stage.from)
        ?? (toolpathSegments.length ? stageWeights[stageWeights.length - 1] : stageWeights[0]);
      const lastCompletedStage = [...stageWeights].reverse().find((stage) => stage.count && stage.to <= targetWeight);
      return currentStage?.count
        ? currentStage.start + Math.min(currentStage.count - 1, Math.floor(
            Math.max(0, Math.min(1, (targetWeight - currentStage.from) / (currentStage.to - currentStage.from || 1))) * (currentStage.count - 1) + 1e-6,
          ))
        : lastCompletedStage?.count ? lastCompletedStage.start + lastCompletedStage.count - 1 : 0;
    };

    const turningSampleTargets = turningStage && turningBeforeSamples.length
      ? turningBeforeSamples.map((before, index) => {
          const indexed = turningAfterSamples[index];
          const after = indexed && Math.abs(indexed.z - before.z) < 1e-5
            ? indexed
            : turningAfterSamples.reduce((nearest, candidate) =>
                Math.abs(candidate.z - before.z) < Math.abs(nearest.z - before.z) ? candidate : nearest,
              turningAfterSamples[0] ?? before);
          return { before, after };
        })
      : [];
    const turningAxis = turningStage
      ? new THREE.Vector3(
          turningStage.axis_direction.x,
          turningStage.axis_direction.y,
          turningStage.axis_direction.z,
        ).normalize()
      : new THREE.Vector3(1, 0, 0);
    const turningAxisOrigin = turningStage
      ? new THREE.Vector3(turningStage.axis_origin.x, turningStage.axis_origin.y, turningStage.axis_origin.z)
      : new THREE.Vector3();
    const turningSampleSpacing = turningSampleTargets.length > 1
      ? Math.max(
          0.02,
          Math.abs(turningSampleTargets[turningSampleTargets.length - 1].before.z - turningSampleTargets[0].before.z)
            / (turningSampleTargets.length - 1),
        )
      : 0.1;
    const turningEncounterRatios = turningSampleTargets.map(({ before }, sampleIndex) => {
      let earliest = Number.POSITIVE_INFINITY;
      for (let index = 0; index < toolpathSegments.length; index += 1) {
        const segment = toolpathSegments[index];
        if (segment.motion !== "cut" || (turningStage && segment.operation_id !== turningStage.operation_id)) continue;
        const start = new THREE.Vector3(segment.x1, segment.y1, segment.z1).sub(turningAxisOrigin).dot(turningAxis);
        const end = new THREE.Vector3(segment.x2, segment.y2, segment.z2).sub(turningAxisOrigin).dot(turningAxis);
        const minimum = Math.min(start, end) - turningSampleSpacing * 0.75;
        const maximum = Math.max(start, end) + turningSampleSpacing * 0.75;
        if (before.z < minimum || before.z > maximum) continue;
        const segmentRatio = Math.abs(end - start) > 1e-6
          ? THREE.MathUtils.clamp((before.z - start) / (end - start), 0, 1)
          : 1;
        earliest = Math.min(
          earliest,
          (cumulativeWeights[index] + segmentWeights[index] * segmentRatio) / totalWeight,
        );
      }
      // Some controller cycles are represented as one semantic command rather
      // than a dense path. Give those samples a deterministic progressive
      // fallback instead of holding all material until the final frame.
      return Number.isFinite(earliest)
        ? earliest
        : 0.08 + 0.82 * sampleIndex / Math.max(turningSampleTargets.length - 1, 1);
    });
    let lastTurningGeometryProgress = -1;
    const updateTurningMaterial = (currentProgress: number) => {
      if (!turningBeforeMesh || !turningSampleTargets.length) return;
      if (Math.abs(currentProgress - lastTurningGeometryProgress) < 0.008 && currentProgress < 0.999) return;
      const dynamicSamples = turningSampleTargets.map(({ before, after }, index) => {
        const encounter = turningEncounterRatios[index];
        const pathProgress = currentProgress >= 0.999
          ? 1
          : THREE.MathUtils.smoothstep(currentProgress, encounter, Math.min(encounter + 0.055, 1));
        // Controller cycles may collapse several physical passes into one IR
        // command. Blend in the operation-wide pass so every changed section
        // evolves continuously instead of waiting for the last frame.
        const localProgress = Math.max(
          pathProgress,
          THREE.MathUtils.smoothstep(currentProgress, 0.04, 0.96),
        );
        return {
          z: THREE.MathUtils.lerp(before.z, after.z, localProgress),
          outer_radius: THREE.MathUtils.lerp(before.outer_radius, after.outer_radius, localProgress),
          inner_radius: THREE.MathUtils.lerp(before.inner_radius, after.inner_radius, localProgress),
        };
      });
      const profile = [
        ...dynamicSamples.map((sample) => new THREE.Vector2(Math.max(sample.outer_radius, 0), sample.z)),
        ...dynamicSamples.slice().reverse().map((sample) => new THREE.Vector2(Math.max(sample.inner_radius, 0), sample.z)),
      ];
      const previousGeometry = turningBeforeMesh.geometry;
      turningBeforeMesh.geometry = new THREE.LatheGeometry(profile, 64);
      turningBeforeMesh.geometry.computeVertexNormals();
      previousGeometry.dispose();
      lastTurningGeometryProgress = currentProgress;
    };

    if (simulation && profileBoundaries.length) {
      const boundary = profileBoundaries[0];
      const localBoundary = boundary.points.map((point) => worldToLocal(point.x, point.y, point.z));
      const insidePolygon = (x: number, y: number) => {
        let inside = false;
        let previous = localBoundary[localBoundary.length - 1];
        for (const current of localBoundary) {
          if ((previous.y > y) !== (current.y > y)) {
            const crossingX = (current.x - previous.x) * (y - previous.y) / (current.y - previous.y) + previous.x;
            if (x < crossingX) inside = !inside;
          }
          previous = current;
        }
        return inside;
      };
      for (let row = 0; row < simulation.surface.rows; row += 1) {
        for (let column = 0; column < simulation.surface.columns; column += 1) {
          const x = simulation.surface.origin.x + column * simulation.surface.resolution_mm;
          const y = simulation.surface.origin.y + row * simulation.surface.resolution_mm;
          if (!insidePolygon(x, y)) outsideProfileIndices.push(row * simulation.surface.columns + column);
        }
      }
      let finalProfileSegment = -1;
      for (let index = 0; index < toolpathSegments.length; index += 1) {
        if (toolpathSegments[index].operation_id === boundary.operation_id && toolpathSegments[index].motion === "cut") {
          finalProfileSegment = index;
        }
      }
      if (finalProfileSegment >= 0) profileDetachWeight = cumulativeWeights[finalProfileSegment + 1];
    }

    const isMaterialCut = (segment: ToolpathSegment) => {
      if (!simulation) return false;
      const tool = operationTools[segment.operation_id];
      return segment.motion === "cut" && tool?.kind !== "chamfer_mill";
    };

    const resetMaterial = () => {
      if (!simulation || !dynamicHeights || !dynamicLowerHeights || !surfacePositions || !lowerSurfacePositions) return;
      dynamicHeights.fill(simulation.surface.top_z);
      dynamicLowerHeights.fill(simulation.surface.bottom_z);
      for (let index = 0; index < dynamicHeights.length; index += 1) {
        const column = index % simulation.surface.columns;
        const row = Math.floor(index / simulation.surface.columns);
        const point = localToScene(
          simulation.surface.origin.x + column * simulation.surface.resolution_mm,
          simulation.surface.origin.y + row * simulation.surface.resolution_mm,
          simulation.surface.top_z,
        );
        const lowerPoint = localToScene(
          simulation.surface.origin.x + column * simulation.surface.resolution_mm,
          simulation.surface.origin.y + row * simulation.surface.resolution_mm,
          simulation.surface.bottom_z,
        );
        surfacePositions.setXYZ(index, point.x, point.y, point.z);
        lowerSurfacePositions.setXYZ(index, lowerPoint.x, lowerPoint.y, lowerPoint.z);
      }
      surfacePositions.needsUpdate = true;
      lowerSurfacePositions.needsUpdate = true;
      processedRatios.fill(0);
      materialProgress = 0;
      applyBaselineMaterial();
    };

    const rebuildMaterialWalls = () => {
      if (!simulation || !simulationMesh || !simulationLowerMesh || !simulationWalls || !dynamicHeights || !dynamicLowerHeights) return;
      const surface = simulation.surface;
      const heights = dynamicHeights;
      const lowerHeights = dynamicLowerHeights;
      const positions: number[] = [];
      const indices: number[] = [];
      const addQuad = (a: THREE.Vector3, b: THREE.Vector3, c: THREE.Vector3, d: THREE.Vector3) => {
        const base = positions.length / 3;
        for (const point of [a, b, c, d]) positions.push(point.x, point.y, point.z);
        indices.push(base, base + 1, base + 2, base, base + 2, base + 3);
      };
      const half = surface.resolution_mm / 2;
      const materialThreshold = 0.02;

      // A through-cut is empty space, not a sheet of material collapsed onto Z-bottom.
      // Rebuild the top triangles so completed holes and detached outside scrap are
      // genuinely open and reveal the sacrificial plate underneath.
      const surfaceIndices: number[] = [];
      for (let row = 0; row < surface.rows - 1; row += 1) {
        for (let column = 0; column < surface.columns - 1; column += 1) {
          const a = row * surface.columns + column;
          const b = a + 1;
          const c = a + surface.columns;
          const d = c + 1;
          if (dynamicHeights[a] - lowerHeights[a] > materialThreshold && dynamicHeights[b] - lowerHeights[b] > materialThreshold && dynamicHeights[c] - lowerHeights[c] > materialThreshold) {
            surfaceIndices.push(a, b, c);
          }
          if (dynamicHeights[b] - lowerHeights[b] > materialThreshold && dynamicHeights[d] - lowerHeights[d] > materialThreshold && dynamicHeights[c] - lowerHeights[c] > materialThreshold) {
            surfaceIndices.push(b, d, c);
          }
        }
      }
      simulationMesh.geometry.setIndex(surfaceIndices);
      simulationMesh.geometry.computeVertexNormals();
      simulationLowerMesh.geometry.setIndex(surfaceIndices);
      simulationLowerMesh.geometry.computeVertexNormals();

      for (let row = 0; row < surface.rows; row += 1) {
        for (let column = 0; column < surface.columns; column += 1) {
          const index = row * surface.columns + column;
          const height = dynamicHeights[index];
          const lowerHeight = lowerHeights[index];
          const x = surface.origin.x + column * surface.resolution_mm;
          const y = surface.origin.y + row * surface.resolution_mm;
          if (column + 1 < surface.columns) {
            const neighbor = dynamicHeights[index + 1];
            if (Math.abs(height - neighbor) > 0.02) {
              const low = Math.min(height, neighbor);
              const high = Math.max(height, neighbor);
              const wallX = x + half;
              addQuad(
                localToScene(wallX, y - half, low), localToScene(wallX, y + half, low),
                localToScene(wallX, y + half, high), localToScene(wallX, y - half, high),
              );
            }
            const lowerNeighbor = lowerHeights[index + 1];
            if (Math.abs(lowerHeight - lowerNeighbor) > 0.02) {
              const low = Math.min(lowerHeight, lowerNeighbor);
              const high = Math.max(lowerHeight, lowerNeighbor);
              const wallX = x + half;
              addQuad(
                localToScene(wallX, y - half, low), localToScene(wallX, y + half, low),
                localToScene(wallX, y + half, high), localToScene(wallX, y - half, high),
              );
            }
          }
          if (row + 1 < surface.rows) {
            const neighbor = dynamicHeights[index + surface.columns];
            if (Math.abs(height - neighbor) > 0.02) {
              const low = Math.min(height, neighbor);
              const high = Math.max(height, neighbor);
              const wallY = y + half;
              addQuad(
                localToScene(x - half, wallY, low), localToScene(x + half, wallY, low),
                localToScene(x + half, wallY, high), localToScene(x - half, wallY, high),
              );
            }
            const lowerNeighbor = lowerHeights[index + surface.columns];
            if (Math.abs(lowerHeight - lowerNeighbor) > 0.02) {
              const low = Math.min(lowerHeight, lowerNeighbor);
              const high = Math.max(lowerHeight, lowerNeighbor);
              const wallY = y + half;
              addQuad(
                localToScene(x - half, wallY, low), localToScene(x + half, wallY, low),
                localToScene(x + half, wallY, high), localToScene(x - half, wallY, high),
              );
            }
          }
        }
      }

      // Recreate only the outside stock walls that still contain material. This
      // keeps the raw blank solid at the start and lets detached scrap disappear.
      const addOutsideWall = (indexA: number, indexB: number) => {
        const heightA = heights[indexA];
        const heightB = heights[indexB];
        const lowerA = lowerHeights[indexA];
        const lowerB = lowerHeights[indexB];
        if (heightA - lowerA <= materialThreshold && heightB - lowerB <= materialThreshold) return;
        const ax = surface.origin.x + (indexA % surface.columns) * surface.resolution_mm;
        const ay = surface.origin.y + Math.floor(indexA / surface.columns) * surface.resolution_mm;
        const bx = surface.origin.x + (indexB % surface.columns) * surface.resolution_mm;
        const by = surface.origin.y + Math.floor(indexB / surface.columns) * surface.resolution_mm;
        addQuad(
          localToScene(ax, ay, lowerA),
          localToScene(bx, by, lowerB),
          localToScene(bx, by, heightB),
          localToScene(ax, ay, heightA),
        );
      };
      for (let column = 0; column < surface.columns - 1; column += 1) {
        addOutsideWall(column, column + 1);
        const bottomRow = (surface.rows - 1) * surface.columns;
        addOutsideWall(bottomRow + column + 1, bottomRow + column);
      }
      for (let row = 0; row < surface.rows - 1; row += 1) {
        addOutsideWall((row + 1) * surface.columns, row * surface.columns);
        addOutsideWall(row * surface.columns + surface.columns - 1, (row + 1) * surface.columns + surface.columns - 1);
      }
      const geometry = simulationWalls.geometry;
      geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
      geometry.setIndex(indices);
      geometry.computeVertexNormals();
    };

    const detachOutsideScrap = () => {
      if (!simulation || !dynamicHeights || !dynamicLowerHeights || !surfacePositions || !lowerSurfacePositions) return;
      for (const index of outsideProfileIndices) {
        dynamicHeights[index] = simulation.surface.bottom_z;
        dynamicLowerHeights[index] = simulation.surface.bottom_z;
        const column = index % simulation.surface.columns;
        const row = Math.floor(index / simulation.surface.columns);
        const point = localToScene(
          simulation.surface.origin.x + column * simulation.surface.resolution_mm,
          simulation.surface.origin.y + row * simulation.surface.resolution_mm,
          simulation.surface.bottom_z,
        );
        surfacePositions.setXYZ(index, point.x, point.y, point.z);
        lowerSurfacePositions.setXYZ(index, point.x, point.y, point.z);
      }
    };

    const removeDisk = (x: number, y: number, cuttingZ: number, radius: number, workAxis: Vec3, toolKind: string) => {
      if (!simulation || !dynamicHeights || !dynamicLowerHeights || !surfacePositions || !lowerSurfacePositions) return;
      const surface = simulation.surface;
      const local = worldToLocal(x, y, cuttingZ);
      const clampedZ = Math.max(surface.bottom_z, Math.min(surface.top_z, local.z));
      const alignment = workAxis.x * surfaceFrame.z.x + workAxis.y * surfaceFrame.z.y + workAxis.z * surfaceFrame.z.z;
      if (Math.abs(alignment) < 0.9) return;
      const columnMin = Math.max(0, Math.floor((local.x - radius - surface.origin.x) / surface.resolution_mm));
      const columnMax = Math.min(surface.columns - 1, Math.ceil((local.x + radius - surface.origin.x) / surface.resolution_mm));
      const rowMin = Math.max(0, Math.floor((local.y - radius - surface.origin.y) / surface.resolution_mm));
      const rowMax = Math.min(surface.rows - 1, Math.ceil((local.y + radius - surface.origin.y) / surface.resolution_mm));
      const radiusSquared = radius * radius;
      for (let row = rowMin; row <= rowMax; row += 1) {
        const cellY = surface.origin.y + row * surface.resolution_mm;
        for (let column = columnMin; column <= columnMax; column += 1) {
          const cellX = surface.origin.x + column * surface.resolution_mm;
          const radialSquared = (cellX - local.x) ** 2 + (cellY - local.y) ** 2;
          if (radialSquared > radiusSquared) continue;
          const ballOffset = toolKind === "ball_end_mill"
            ? radius - Math.sqrt(Math.max(radiusSquared - radialSquared, 0))
            : 0;
          const effectiveZ = alignment > 0 ? clampedZ + ballOffset : clampedZ - ballOffset;
          const index = row * surface.columns + column;
          if (alignment > 0 && effectiveZ < dynamicHeights[index]) {
            dynamicHeights[index] = Math.max(dynamicLowerHeights[index], effectiveZ);
            const point = localToScene(cellX, cellY, dynamicHeights[index]);
            surfacePositions.setXYZ(index, point.x, point.y, point.z);
          } else if (alignment < 0 && effectiveZ > dynamicLowerHeights[index]) {
            dynamicLowerHeights[index] = Math.min(dynamicHeights[index], effectiveZ);
            const point = localToScene(cellX, cellY, dynamicLowerHeights[index]);
            lowerSurfacePositions.setXYZ(index, point.x, point.y, point.z);
          }
        }
      }
    };

    const applyMaterialSegment = (segment: ToolpathSegment, fromRatio: number, toRatio: number) => {
      if (!simulation || !isMaterialCut(segment) || toRatio <= fromRatio) return;
      const distance = Math.hypot(segment.x2 - segment.x1, segment.y2 - segment.y1, segment.z2 - segment.z1);
      const span = distance * (toRatio - fromRatio);
      const sampleCount = Math.max(1, Math.ceil(span / Math.max(simulation.surface.resolution_mm * 0.5, 0.1)));
      const tool = operationTools[segment.operation_id] ?? { diameter_mm: 6, stickout_mm: 25 };
      const workAxis = segment.work_axis ?? { x: 0, y: 0, z: 1 };
      for (let sample = 1; sample <= sampleCount; sample += 1) {
        const ratio = THREE.MathUtils.lerp(fromRatio, toRatio, sample / sampleCount);
        const localCuttingZ = segment.local_z1 !== undefined && segment.local_z2 !== undefined
          ? THREE.MathUtils.lerp(segment.local_z1, segment.local_z2, ratio)
          : Number.NEGATIVE_INFINITY;
        if (segment.local_stock_top_z !== undefined && localCuttingZ > segment.local_stock_top_z + 1e-6) continue;
        removeDisk(
          THREE.MathUtils.lerp(segment.x1, segment.x2, ratio),
          THREE.MathUtils.lerp(segment.y1, segment.y2, ratio),
          THREE.MathUtils.lerp(segment.z1, segment.z2, ratio),
          tool.diameter_mm / 2,
          workAxis,
          tool.kind,
        );
      }
    };

    function applyBaselineMaterial() {
      if (!simulation || !initialToolpathSegments.length || !dynamicHeights || !dynamicLowerHeights || !surfacePositions || !lowerSurfacePositions) return;
      for (const segment of initialToolpathSegments) applyMaterialSegment(segment, 0, 1);
      const baselineOperationIds = new Set(initialToolpathSegments.map((segment) => segment.operation_id));
      if (profileBoundaries.some((boundary) => baselineOperationIds.has(boundary.operation_id))) detachOutsideScrap();
      surfacePositions.needsUpdate = true;
      lowerSurfacePositions.needsUpdate = true;
    }

    const applyFinalSurface = () => {
      if (!simulation || !dynamicHeights || !dynamicLowerHeights || !surfacePositions || !lowerSurfacePositions) return;
      const surface = simulation.surface;
      const finalLowerHeights = surface.lower_heights
        ?? new Array(surface.columns * surface.rows).fill(surface.bottom_z);
      dynamicHeights.set(surface.heights);
      dynamicLowerHeights.set(finalLowerHeights);
      for (let row = 0; row < surface.rows; row += 1) {
        for (let column = 0; column < surface.columns; column += 1) {
          const index = row * surface.columns + column;
          const x = surface.origin.x + column * surface.resolution_mm;
          const y = surface.origin.y + row * surface.resolution_mm;
          const upperPoint = localToScene(x, y, dynamicHeights[index]);
          const lowerPoint = localToScene(x, y, dynamicLowerHeights[index]);
          surfacePositions.setXYZ(index, upperPoint.x, upperPoint.y, upperPoint.z);
          lowerSurfacePositions.setXYZ(index, lowerPoint.x, lowerPoint.y, lowerPoint.z);
        }
      }
    };

    const updateMaterial = (currentProgress: number, targetWeight: number) => {
      if (!simulation || !dynamicHeights || !dynamicLowerHeights || !surfacePositions || !lowerSurfacePositions || !animateToolpath) return;
      if (Math.abs(currentProgress - materialProgress) < 1e-6) return;
      if (currentProgress + 1e-6 < materialProgress) resetMaterial();
      for (let index = 0; index < toolpathSegments.length; index += 1) {
        const desiredRatio = targetWeight >= cumulativeWeights[index + 1]
          ? 1
          : targetWeight <= cumulativeWeights[index]
            ? 0
            : (targetWeight - cumulativeWeights[index]) / segmentWeights[index];
        if (desiredRatio > processedRatios[index]) {
          applyMaterialSegment(toolpathSegments[index], processedRatios[index], desiredRatio);
          processedRatios[index] = desiredRatio;
        }
      }
      if (targetWeight >= profileDetachWeight) detachOutsideScrap();
      // The browser replay is intentionally incremental and can accumulate
      // sampling stripes. At the end of the last operation, use the backend's
      // authoritative cumulative snapshot so the final result is deterministic.
      if (isFinalOperation && currentProgress >= 0.999) applyFinalSurface();
      surfacePositions.needsUpdate = true;
      lowerSurfacePositions.needsUpdate = true;
      if (Math.abs(currentProgress - lastNormalsProgress) >= 0.015 || currentProgress >= 0.999 || currentProgress === 0) {
        rebuildMaterialWalls();
        lastNormalsProgress = currentProgress;
      }
      materialProgress = currentProgress;
    };

    const updatePlaybackScene = (currentProgress: number) => {
      if (!playbackTool || !activePath || !toolpathSegments.length) return;
      const targetWeight = currentProgress * totalWeight;
      updateMaterial(currentProgress, targetWeight);
      let segmentIndex = cumulativeWeights.findIndex((value, index) => index > 0 && value >= targetWeight) - 1;
      if (segmentIndex < 0) segmentIndex = toolpathSegments.length - 1;
      segmentIndex = Math.min(segmentIndex, toolpathSegments.length - 1);
      const segment = toolpathSegments[segmentIndex];
      for (const fixture of fixtureMeshes) {
        fixture.mesh.visible = !fixture.setupId || !segment.setup_id || fixture.setupId === segment.setup_id;
      }
      defectPointCloud?.geometry.dispose();
      (defectPointCloud?.material as THREE.Material | undefined)?.dispose();
      for (const mesh of defectRegionMeshes) {
        mesh.geometry.dispose();
        (mesh.material as THREE.Material).dispose();
      }
      const startWeight = cumulativeWeights[segmentIndex];
      const ratio = Math.min(1, Math.max(0, (targetWeight - startWeight) / segmentWeights[segmentIndex]));
      const tip = new THREE.Vector3(
        THREE.MathUtils.lerp(segment.x1, segment.x2, ratio) - modelCenter.x,
        THREE.MathUtils.lerp(segment.y1, segment.y2, ratio) - modelCenter.y,
        THREE.MathUtils.lerp(segment.z1, segment.z2, ratio) - modelCenter.z,
      );
      const axisValue = segment.work_axis ?? { x: 0, y: 0, z: 1 };
      const axis = new THREE.Vector3(axisValue.x, axisValue.y, axisValue.z).normalize();
      const tool = operationTools[segment.operation_id] ?? { diameter_mm: 6, stickout_mm: 25, holder_diameter_mm: 25, kind: "end_mill", drill_point_angle_deg: 118 };
      const pointAngle = tool.kind === "chamfer_mill" ? 90 : tool.drill_point_angle_deg;
      const drillTipLength = tool.kind === "drill" || tool.kind === "chamfer_mill"
        ? tool.diameter_mm / 2 / Math.tan(THREE.MathUtils.degToRad(pointAngle / 2))
        : 0;
      // Material removal still uses the catalog dimensions. These caps affect
      // only the rendered assembly so a long holder cannot hide a small part.
      const visualDiameter = Math.min(
        Math.max(tool.diameter_mm, 0.5),
        Math.max(viewSize * 0.12, 0.8),
      );
      const visualStickout = Math.min(
        Math.max(tool.stickout_mm, visualDiameter * 1.5),
        Math.max(viewSize * 0.28, visualDiameter * 2.2, 2),
      );
      const cutterLength = Math.max(1, visualStickout - drillTipLength);
      if (playbackCutter) {
        playbackCutter.scale.set(visualDiameter, cutterLength, visualDiameter);
        playbackCutter.position.set(0, drillTipLength + cutterLength / 2, 0);
        (playbackCutter.material as THREE.MeshStandardMaterial).emissive.set(segment.motion === "cut" ? 0x17362f : 0x172129);
      }
      if (playbackDrillTip) {
        playbackDrillTip.visible = tool.kind === "drill" || tool.kind === "chamfer_mill";
        playbackDrillTip.scale.set(visualDiameter, Math.max(drillTipLength, 0.01), visualDiameter);
        playbackDrillTip.position.set(0, drillTipLength / 2, 0);
        (playbackDrillTip.material as THREE.MeshStandardMaterial).emissive.set(segment.motion === "cut" ? 0x17362f : 0x172129);
      }
      if (playbackHolder) {
        const holderDiameter = Math.min(
          Math.max(tool.holder_diameter_mm, visualDiameter),
          Math.max(viewSize * 0.16, visualDiameter * 1.35, 1.2),
        );
        const holderLength = Math.max(viewSize * 0.2, visualDiameter * 1.8, 1.5);
        playbackHolder.scale.set(holderDiameter, holderLength, holderDiameter);
        playbackHolder.position.set(0, visualStickout + holderLength / 2, 0);
      }
      playbackTool.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), axis);
      playbackTool.position.copy(tip);
      const activePositions = activePath.geometry.getAttribute("position") as THREE.BufferAttribute;
      activePositions.setXYZ(0, segment.x1 - modelCenter.x, segment.y1 - modelCenter.y, segment.z1 - modelCenter.z);
      activePositions.setXYZ(1, tip.x, tip.y, tip.z);
      activePositions.needsUpdate = true;
      (activePath.material as THREE.LineBasicMaterial).color.set(segment.motion === "cut" ? 0x8affda : 0x78bde9);
      trailPath?.geometry.setDrawRange(0, Math.min(toolpathSegments.length * 2, (segmentIndex + 1) * 2));
      if (simulationMesh) simulationMesh.visible = true;
      if (simulationLowerMesh) simulationLowerMesh.visible = true;
      if (lastReportedSegment !== segmentIndex) {
        lastReportedSegment = segmentIndex;
        setActiveMotion({ operation: segment.operation_id, setup: segment.setup_id, motion: segment.motion, removesMaterial: isMaterialCut(segment) });
      }
    };

    const updateFormingScene = (currentProgress: number) => {
      if (!formingPreview || !model) return;
      const timeline = Math.min(currentProgress * formingPreview.stages.length, formingPreview.stages.length - 1e-6);
      const stageIndex = Math.floor(timeline);
      const stageProgress = timeline - stageIndex;
      const stage = formingPreview.stages[stageIndex];
      if (!stage) return;
      const eased = stageProgress * stageProgress * (3 - 2 * stageProgress);
      const normalizedFactor = THREE.MathUtils.lerp(stage.start_factor, stage.end_factor, eased);
      const flatFactor = Math.min(1, formingPreview.nominal_thickness_mm / Math.max(formingPreview.formed_depth_mm, formingPreview.nominal_thickness_mm));
      const factor = THREE.MathUtils.lerp(flatFactor, 1, normalizedFactor);
      const isLayout = stage.process === "sheet_flat_pattern";
      const isBlanking = stage.process === "sheet_blanking";
      const isPressing = stage.process === "sheet_preforming" || stage.process === "sheet_final_forming";
      const isFinishing = stage.process === "sheet_deburring";
      const isInspecting = stage.process === "sheet_inspection";

      model.visible = !isLayout || stageProgress > 0.72;
      const modelMaterial = model.material as THREE.MeshPhysicalMaterial;
      modelMaterial.opacity = isLayout ? Math.max(0, (stageProgress - 0.72) / 0.28) * 0.28 : isBlanking ? 0.3 + eased * 0.7 : 1;
      modelMaterial.depthWrite = modelMaterial.opacity > 0.95;
      if (formingStock) {
        formingStock.visible = isLayout || isBlanking;
        const stockMaterial = formingStock.material as THREE.MeshPhysicalMaterial;
        stockMaterial.opacity = isLayout ? 0.92 : Math.max(0.08, 0.92 * (1 - eased));
        formingStock.scale.setScalar(isBlanking ? 1 - eased * 0.015 : 1);
      }
      if (formingCutOutline) {
        formingCutOutline.visible = isBlanking && !formingCutSegmentPoints.length;
        formingCutOutline.geometry.setDrawRange(0, Math.max(1, Math.ceil(eased * 4)));
      }
      if (formingCutPaths) {
        formingCutPaths.visible = isBlanking;
        const completedVertices = Math.min(
          formingCutSegmentPoints.length,
          Math.floor(eased * (formingCutSegmentPoints.length / 2)) * 2,
        );
        formingCutPaths.geometry.setDrawRange(0, completedVertices);
      }
      if (formingLowerDie) {
        formingLowerDie.visible = isPressing;
        formingLowerDie.position.y = viewSize * (0.2 - eased * 0.12);
        (formingLowerDie.material as THREE.MeshPhysicalMaterial).opacity = 0.08 + eased * 0.1;
      }
      if (Math.abs(factor - lastFormingFactor) >= 0.001) {
        // The geometry is centered at the origin, so an object transform is
        // equivalent to rewriting every vertex's Y coordinate.  Leaving that
        // work to the GPU keeps large (30+ MB) STL previews interactive.
        model.scale.y = factor;
        modelMaterial.color.setHSL(0.55 - normalizedFactor * 0.02, 0.07 + normalizedFactor * 0.03, 0.52 - normalizedFactor * 0.08);
        lastFormingFactor = factor;
      }
      if (formingActuator) {
        formingActuator.visible = !isLayout;
        if (isPressing) {
          formingActuator.scale.set(viewSize * 0.92, Math.max(viewSize * 0.045, 1), viewSize * 0.72);
          formingActuator.position.set(0, -viewSize * (0.32 - eased * 0.22), 0);
          formingActuator.rotation.set(0, 0, 0);
          (formingActuator.material as THREE.MeshPhysicalMaterial).color.set(0x5a83d8);
          (formingActuator.material as THREE.MeshPhysicalMaterial).opacity = 0.24;
        } else {
          formingActuator.scale.set(viewSize * (isBlanking ? 0.025 : 0.018), viewSize * 0.16, viewSize * (isFinishing ? 0.055 : 0.025));
          if (isBlanking && formingCutSegmentPoints.length >= 2) {
            const segmentCount = formingCutSegmentPoints.length / 2;
            const path = Math.min(eased * segmentCount, segmentCount - 1e-6);
            const pointIndex = Math.floor(path) * 2;
            formingActuator.position.lerpVectors(
              formingCutSegmentPoints[pointIndex],
              formingCutSegmentPoints[pointIndex + 1],
              path % 1,
            );
          } else if (isBlanking) {
            const sweep = [
              new THREE.Vector3(-viewSize * 0.43, -viewSize * 0.12, -viewSize * 0.28),
              new THREE.Vector3(viewSize * 0.43, -viewSize * 0.12, -viewSize * 0.28),
              new THREE.Vector3(viewSize * 0.43, -viewSize * 0.12, viewSize * 0.28),
              new THREE.Vector3(-viewSize * 0.43, -viewSize * 0.12, viewSize * 0.28),
              new THREE.Vector3(-viewSize * 0.43, -viewSize * 0.12, -viewSize * 0.28),
            ];
            const path = Math.min(eased * 4, 3.9999);
            formingActuator.position.lerpVectors(sweep[Math.floor(path)], sweep[Math.floor(path) + 1], path % 1);
          } else {
            formingActuator.position.set(THREE.MathUtils.lerp(-viewSize * 0.48, viewSize * 0.48, eased), -viewSize * 0.08, 0);
          }
          formingActuator.rotation.set(0, 0, isFinishing ? Math.PI / 2 : 0);
          (formingActuator.material as THREE.MeshPhysicalMaterial).color.set(
            isInspecting ? 0x45cfa1 : isFinishing ? 0xe0a64f : 0x55b8d0,
          );
          (formingActuator.material as THREE.MeshPhysicalMaterial).opacity = isInspecting ? 0.48 : 0.72;
        }
      }
    };

    const animate = (time: number) => {
      const elapsed = Math.min((time - lastFrameTime) / 1000, 0.1);
      lastFrameTime = time;
      if (animateToolpath && playbackRef.current.playing && (toolpathSegments.length || formingPreview)) {
        const duration = formingPreview ? 18 : turningStage ? 8 : materialSnapshotUrls.length ? 12 * Math.max(1, materialSnapshotStages.length) : 30;
        const proposedProgress = Math.min(1, playbackRef.current.progress + elapsed * playbackRef.current.speed / duration);
        const nextSnapshotIndex = snapshotIndexForProgress(proposedProgress);
        if (materialSnapshotUrls.length && !materialSnapshotMeshes[nextSnapshotIndex]) requestMaterialSnapshot(nextSnapshotIndex);
        const nextProgress = materialSnapshotUrls.length && !materialSnapshotMeshes[nextSnapshotIndex]
          ? playbackRef.current.progress : proposedProgress;
        playbackRef.current.progress = nextProgress;
        progressByModeRef.current[playbackKey] = nextProgress;
        setProgress(nextProgress);
        if (nextProgress >= 1) {
          playbackRef.current.playing = false;
          setPlaying(false);
        }
      }
      if (animateToolpath) updatePlaybackScene(playbackRef.current.progress);
      if (animateToolpath) updateFormingScene(playbackRef.current.progress);
      if (materialSnapshotUrls.length) {
        const requestedIndex = snapshotIndexForProgress(playbackRef.current.progress);
        requestMaterialSnapshot(requestedIndex);
        requestMaterialSnapshot(Math.min(requestedIndex + 1, materialSnapshotMeshes.length - 1));
        let visibleIndex = requestedIndex;
        while (visibleIndex > 0 && !materialSnapshotMeshes[visibleIndex]) visibleIndex -= 1;
        materialSnapshotMeshes.forEach((snapshot, index) => {
          if (snapshot) snapshot.visible = index === visibleIndex;
        });
        if (materialSnapshotMeshes[requestedIndex]) {
          materialSnapshotMeshes.forEach((snapshot, index) => {
            if (!snapshot || Math.abs(index - requestedIndex) <= 3) return;
            scene.remove(snapshot);
            snapshot.geometry.dispose();
            (snapshot.material as THREE.Material).dispose();
            materialSnapshotMeshes[index] = null;
          });
        }
        if (model) model.visible = false;
        if (cadEdges) cadEdges.visible = false;
        if (simulationMesh) simulationMesh.visible = false;
        if (simulationLowerMesh) simulationLowerMesh.visible = false;
        if (simulationWalls) simulationWalls.visible = false;
      }
      if (turningBeforeMesh && turningAfterMesh) {
        const turningProgress = animateToolpath ? playbackRef.current.progress : 1;
        updateTurningMaterial(turningProgress);
        const beforeMaterial = turningBeforeMesh.material as THREE.MeshPhysicalMaterial;
        beforeMaterial.opacity = 0.96;
        beforeMaterial.visible = true;
        beforeMaterial.depthWrite = true;
        // The authoritative final mesh remains available for bounds checking,
        // while the visible IPW is now the continuously rebuilt dynamic mesh.
        turningAfterMesh.visible = false;
      }
      if (camoticsMesh) camoticsMesh.visible = materialSnapshotUrls.length === 0 && playbackRef.current.progress >= 0.999;
      controls.update();
      renderer.render(scene, camera);
      axisGroup.quaternion.copy(camera.quaternion).invert();
      axisRenderer.render(axisScene, axisCamera);
      animation = requestAnimationFrame(animate);
    };
    resetMaterial();
    rebuildMaterialWalls();
    animate(performance.now());
    return () => {
      if (model) {
        cameraMemory[cameraKey] = {
          position: camera.position.toArray() as [number, number, number],
          target: controls.target.toArray() as [number, number, number],
          zoom: camera.zoom,
        };
      }
      disposed = true;
      cancelAnimationFrame(animation);
      observer.disconnect();
      renderer.domElement.removeEventListener("pointerdown", recordPointerDown, true);
      renderer.domElement.removeEventListener("pointerup", pickFeature);
      controls.dispose();
      renderer.dispose();
      axisRenderer.dispose();
      axisGroup.traverse((object) => {
        if (object instanceof THREE.Mesh || object instanceof THREE.Line) {
          object.geometry.dispose();
          const materials = Array.isArray(object.material) ? object.material : [object.material];
          for (const material of materials) material.dispose();
        }
      });
      for (const resource of axisLabelResources) {
        resource.sprite.material.dispose();
        resource.texture.dispose();
      }
      environmentTarget.dispose();
      pmremGenerator.dispose();
      model?.geometry.dispose();
      (model?.material as THREE.Material | undefined)?.dispose();
      for (const snapshot of materialSnapshotMeshes) {
        snapshot?.geometry.dispose();
        (snapshot?.material as THREE.Material | undefined)?.dispose();
      }
      formingActuator?.geometry.dispose();
      (formingActuator?.material as THREE.Material | undefined)?.dispose();
      formingStock?.geometry.dispose();
      (formingStock?.material as THREE.Material | undefined)?.dispose();
      formingLowerDie?.geometry.dispose();
      (formingLowerDie?.material as THREE.Material | undefined)?.dispose();
      formingCutOutline?.geometry.dispose();
      (formingCutOutline?.material as THREE.Material | undefined)?.dispose();
      formingCutPaths?.geometry.dispose();
      (formingCutPaths?.material as THREE.Material | undefined)?.dispose();
      simulationMesh?.geometry.dispose();
      (simulationMesh?.material as THREE.Material | undefined)?.dispose();
      simulationLowerMesh?.geometry.dispose();
      (simulationLowerMesh?.material as THREE.Material | undefined)?.dispose();
      simulationWalls?.geometry.dispose();
      (simulationWalls?.material as THREE.Material | undefined)?.dispose();
      turningBeforeMesh?.geometry.dispose();
      (turningBeforeMesh?.material as THREE.Material | undefined)?.dispose();
      turningAfterMesh?.geometry.dispose();
      (turningAfterMesh?.material as THREE.Material | undefined)?.dispose();
      camoticsMesh?.geometry.dispose();
      (camoticsMesh?.material as THREE.Material | undefined)?.dispose();
      playbackCutter?.geometry.dispose();
      (playbackCutter?.material as THREE.Material | undefined)?.dispose();
      playbackDrillTip?.geometry.dispose();
      (playbackDrillTip?.material as THREE.Material | undefined)?.dispose();
      playbackHolder?.geometry.dispose();
      (playbackHolder?.material as THREE.Material | undefined)?.dispose();
      activePath?.geometry.dispose();
      (activePath?.material as THREE.Material | undefined)?.dispose();
      trailPath?.geometry.dispose();
      (trailPath?.material as THREE.Material | undefined)?.dispose();
      cadEdges?.geometry.dispose();
      (cadEdges?.material as THREE.Material | undefined)?.dispose();
      floorGrid.geometry.dispose();
      for (const material of floorMaterials) material.dispose();
      for (const fixture of fixtureMeshes) {
        fixture.mesh.geometry.dispose();
        (fixture.mesh.material as THREE.Material).dispose();
      }
      for (const marker of markersRef.current.values()) {
        marker.geometry.dispose();
        (marker.material as THREE.Material).dispose();
      }
      for (const child of markerGroup.children) {
        if (child instanceof THREE.LineSegments) {
          child.geometry.dispose();
          (child.material as THREE.Material).dispose();
        }
      }
      markersRef.current.clear();
      viewApiRef.current = null;
      host.removeChild(renderer.domElement);
      axisHost.removeChild(axisRenderer.domElement);
    };
  }, [activeOperationId, animateToolpath, cameraContentKey, cameraKey, camoticsSurface, features, fixtureComponents, formingPreview, initialToolpathSegments, isFinalOperation, materialSnapshotStages, materialSnapshotUrls, modelUrl, operationTools, playbackKey, profileBoundaries, simulation, spatialDefects, toolpathSegments, topologyEdges, turningStage, viewMode, workAxis]);

  const activeTool = activeMotion ? operationTools[activeMotion.operation] : undefined;
  const activeFormingStage = formingPreview?.stages.length
    ? formingPreview.stages[
        Math.min(
          Math.floor(progress * formingPreview.stages.length),
          formingPreview.stages.length - 1,
        )
      ]
    : undefined;
  const formingStateLabels: Record<string, string> = {
    sheet_flat_pattern: "原始矩形板料",
    sheet_blanking: "切除余料，得到平面展开件",
    sheet_preforming: "预成形在制品",
    sheet_final_forming: "终成形样品",
    sheet_deburring: "去毛刺后的成品",
    sheet_inspection: "检测中的最终样品",
  };
  const formingActivityLabels: Record<string, string> = {
    sheet_flat_pattern: "● 正在准备原始板料",
    sheet_blanking: "● 切割头正在沿 STEP 轮廓加工孔、槽与外形",
    sheet_preforming: "● 预成形模具正在闭合",
    sheet_final_forming: "● 终成形模具正在闭合",
    sheet_deburring: "● 正在进行边缘处理",
    sheet_inspection: "● 正在扫描最终样品",
  };
  const playbackChapters = formingPreview
    ? formingPreview.stages.map((stage, index) => ({ id: stage.operation_id, progress: index / formingPreview.stages.length }))
    : (() => {
        const weights = toolpathSegments.map((segment) => Math.max(0.01,
          Math.hypot(segment.x2 - segment.x1, segment.y2 - segment.y1, segment.z2 - segment.z1) / (segment.motion === "rapid" ? 4 : 1),
        ));
        const total = weights.reduce((sum, weight) => sum + weight, 0) || 1;
        let weight = 0;
        const chapters: { id: string; progress: number }[] = [];
        toolpathSegments.forEach((segment, index) => {
          if (!chapters.some((chapter) => chapter.id === segment.operation_id)) {
            chapters.push({ id: segment.operation_id, progress: weight / total });
          }
          weight += weights[index];
        });
        return chapters;
      })();
  const visibleActiveDefect = activeDefect
    ? spatialDefects?.regions.find((region) => region.id === activeDefect.id) ?? null
    : null;
  const modeClass = { "特征": "features", "工艺": "process", "刀路": "toolpath", "仿真": "simulation" }[viewMode];
  const modeTitle = simulationBlocked ? "原始零件" : { "特征": "特征识别", "工艺": "工艺规划", "刀路": "刀路结果", "仿真": "加工仿真" }[viewMode];
  const modeDetail = simulationBlocked ? "整件结果未通过检查，当前显示原始 3D"
    : viewMode === "特征"
    ? `${features.length} 个制造特征 · 点击彩色区域查看`
    : viewMode === "工艺"
      ? activeOperationId ? `${activeOperationId} · ${activeOperationLabel || "当前工序"}` : "请选择一道工序"
      : viewMode === "刀路"
        ? toolpathLoaded ? `${toolpathSegments.length} 段运动轨迹` : "正在加载刀路数据"
        : simulation || formingPreview ? "材料去除过程与安全校验" : "等待刀路与仿真结果";

  return (
    <div className={`model-viewer mode-${modeClass}`} ref={hostRef}>
      <div className="orientation-axis" ref={axisHostRef} aria-label="视图坐标轴"><small>视图坐标</small></div>
      <div className="viewer-badge"><strong>{modeTitle}</strong><small>{modeDetail}</small></div>
      {viewMode === "特征" && <div className="feature-view-legend"><span><i className="hole" />孔</span><span><i className="pocket" />型腔</span><span><i className="slot" />槽</span><span><i className="profile" />内轮廓</span></div>}
      {viewMode === "工艺" && <div className="process-view-legend"><i />当前工序加工区域{workAxis && <small>方向 X{workAxis.x.toFixed(0)} Y{workAxis.y.toFixed(0)} Z{workAxis.z.toFixed(0)}</small>}</div>}
      {(viewMode === "刀路" || viewMode === "仿真") && <div className="viewer-legend">{formingPreview ? <>原始板料 <i className="selected" />落料件 → 预成形 → 终成形样品</> : <><i />快速移动 <i className="selected" />切削轨迹 · 已完成轨迹自动淡化</>}</div>}
      {spatialDefects && spatialDefects.regions.length > 0 && <div className="defect-legend"><span><i className="overcut" />过切</span><span><i className="rest" />残料</span><small>点击色块定位责任工序</small></div>}
      {visibleActiveDefect && <div className={`defect-focus-card ${visibleActiveDefect.kind}`}>
        <button aria-label="关闭缺陷详情" onClick={() => setActiveDefect(null)}>×</button>
        <small>{visibleActiveDefect.id} · {visibleActiveDefect.kind === "overcut" ? "过切区域" : "残料区域"}</small>
        <strong>{visibleActiveDefect.volume_mm3.toFixed(2)} mm³</strong>
        <span>最大偏差 {visibleActiveDefect.max_deviation_mm.toFixed(2)} mm</span>
        {visibleActiveDefect.attribution[0] && <em>候选工序 {visibleActiveDefect.attribution[0].operation_id} · 置信度 {Math.round(visibleActiveDefect.attribution[0].confidence * 100)}%</em>}
      </div>}
      {animateToolpath && activeMotion && <div className="simulation-stage-card">
        <span>{activeMotion.setup} · {activeMotion.operation}</span>
        <strong>{activeTool?.name ?? "加工工序"}</strong>
        <small>{activeTool?.tool_name ?? "刀具"}{activeTool?.spindle_rpm ? ` · ${activeTool.spindle_rpm} RPM` : ""}{activeTool?.feed_rate_mm_min ? ` · F${activeTool.feed_rate_mm_min}` : ""}</small>
        <em className={activeMotion.motion}>{activeMotion.motion === "cut" ? activeMotion.removesMaterial ? "● 正在切削" : "● 成形轨迹" : "→ 快速移动"}</em>
      </div>}
      {animateToolpath && activeFormingStage && <div className="simulation-stage-card forming-stage-card">
        <span>FORMING-1 · {activeFormingStage.operation_id}</span>
        <strong>{activeFormingStage.name}</strong>
        <small>在制品：{formingStateLabels[activeFormingStage.process] ?? activeFormingStage.process} · {Math.round(activeFormingStage.start_factor * 100)}% → {Math.round(activeFormingStage.end_factor * 100)}% 成形深度</small>
        <em>{formingActivityLabels[activeFormingStage.process] ?? "● 工艺过程预览"}</em>
      </div>}
      {(simulation || formingPreview) && <div className="cad-view-controls" aria-label="三维视图控制">
        <div><button onClick={() => viewApiRef.current?.setView("iso")}>轴测</button><button onClick={() => viewApiRef.current?.setView("top")}>俯视</button><button onClick={() => viewApiRef.current?.setView("front")}>前视</button><button onClick={() => viewApiRef.current?.setView("fit")}>适应</button></div>
        {!formingPreview && <div>
          <button className={targetVisible ? "active" : ""} onClick={() => { const value = !targetVisible; targetVisibleRef.current = value; setTargetVisible(value); viewApiRef.current?.setTargetVisible(value); }}>目标件</button>
          <button className={toolVisible ? "active" : ""} onClick={() => { const value = !toolVisible; setToolVisible(value); viewApiRef.current?.setToolVisible(value); }}>刀具</button>
          <button className={trailVisible ? "active" : ""} onClick={() => { const value = !trailVisible; setTrailVisible(value); viewApiRef.current?.setTrailVisible(value); }}>轨迹</button>
        </div>}
      </div>}
      {animateToolpath && !formingPreview && toolpathLoaded && activeOperationId && toolpathSegments.length === 0 && <div className="empty-toolpath-notice">
        {materialSnapshotStages.length ? "当前工序的材料帧已就绪，但有效切削刀路尚未生成；请检查工序预生成结果。" : "当前任务没有可播放的有效 FreeCAD 切削刀路"}
      </div>}
      {viewMode === "仿真" && snapshotLoadError && <div className="empty-toolpath-notice">{snapshotLoadError}</div>}
      {animateToolpath && (toolpathSegments.length > 0 || formingPreview) && <div className="playback-controls">
        {!formingPreview && <div className="playback-mode-toggle" aria-label="播放模式">
          <button className={playbackMode === "single" ? "active" : ""} onClick={() => onPlaybackModeChange?.("single")}>单工序</button>
          <button className={playbackMode === "cumulative" ? "active" : ""} onClick={() => onPlaybackModeChange?.("cumulative")}>累计</button>
        </div>}
        <button onClick={() => updatePlaying(!playing)}>{playing ? "❚❚ 暂停" : "▶ 播放"}</button>
        <button onClick={() => updateProgress(0)}>↺ 重播</button>
        <input aria-label="仿真进度" type="range" min="0" max="1000" value={Math.round(progress * 1000)} onChange={(event) => updateProgress(Number(event.target.value) / 1000)} />
        <span>{Math.round(progress * 100)}%</span>
        <select aria-label="播放速度" value={speed} onChange={(event) => updateSpeed(Number(event.target.value))}>
          <option value="1">1×</option><option value="5">5×</option><option value="20">20×</option>
        </select>
        <div className="playback-chapters" aria-label="工序时间轴">{playbackChapters.map((chapter) => <button type="button" key={chapter.id} className={(activeFormingStage?.operation_id ?? activeMotion?.operation) === chapter.id ? "active" : ""} title={`跳转到 ${chapter.id}`} aria-label={`跳转到 ${chapter.id}`} onClick={() => updateProgress(Math.min(1, chapter.progress + 1e-6))} />)}</div>
        <em>{activeFormingStage ? `${activeFormingStage.operation_id} · ${activeFormingStage.name}` : activeMotion ? `${playbackMode === "single" ? "单工序" : "累计"} · ${activeMotion.operation} · ${activeMotion.motion === "cut" ? activeMotion.removesMaterial ? "切削 · 几何余料逐帧展示" : "轨迹回放" : "快移 · 不去除材料"}` : playbackMode === "single" ? "单工序 · 从本工序加工前余料开始" : "累计 · 从原始毛坯开始"}</em>
      </div>}
    </div>
  );
}
