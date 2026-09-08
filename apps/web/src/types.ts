export type Vec3 = { x: number; y: number; z: number };

export type PlanarFeature = {
  id: string;
  area: number;
  center: Vec3;
  normal: Vec3;
  bounds: Bounds | null;
  wire_count: number;
  adjacent_edge_count: number;
  rising_edge_count: number;
  falling_edge_count: number;
  level_edge_count: number;
  free_edge_count: number;
};

export type Bounds = { minimum: Vec3; maximum: Vec3; size: Vec3 };

export type CylindricalFeature = {
  id: string;
  kind: "hole" | "boss" | "cylinder";
  radius: number;
  diameter: number;
  length: number;
  center: Vec3;
  axis: Vec3;
  angular_span_degrees: number;
  source_face_ids: string[];
  segment_count: number;
  end_type: "through" | "blind" | "unknown";
  access_direction: Vec3 | null;
  confidence: number;
  review_state: "accepted" | "review" | "excluded";
  review_reasons: string[];
};

export type PrismaticFeature = {
  id: string;
  kind: "pocket" | "slot";
  source_face_id: string;
  center: Vec3;
  bounds: Bounds;
  access_direction: Vec3;
  length: number;
  width: number;
  depth: number;
  open_sides: number;
  confidence: number;
  review_state: "accepted" | "review" | "excluded";
  review_reasons: string[];
};

export type ManufacturingFeature = CylindricalFeature | PrismaticFeature;

export type ToolpathSegment = {
  operation_id: string;
  motion: "rapid" | "cut";
  x1: number; y1: number; z1: number;
  x2: number; y2: number; z2: number;
  local_z1?: number; local_z2?: number;
  setup_id?: string;
  work_axis?: Vec3;
};

export type CamResult = {
  status: "completed";
  engine: string;
  engine_version: string;
  operation_backend: "native";
  native_operation_types: Record<string, string>;
  postprocessor: string;
  generated_operations: string[];
  skipped: string[];
  path_command_count: number;
  preview_segments: ToolpathSegment[];
  profile_boundaries: { operation_id: string; setup_id: string; work_axis: Vec3; points: Vec3[] }[];
  simulation_backend?: "camotics-per-setup" | "height-field-fallback";
  camotics_surfaces?: { setup_id: string; file: string; engine: "CAMotics"; resolution_mm: number; frame: { x: Vec3; y: Vec3; z: Vec3 } }[];
  files: { freecad: string; gcode: string; preview: string; verification: string; simulation: string; collision: string; camotics?: string[] };
  verification: VerificationResult;
  simulation: SimulationResult;
  collision: CollisionResult;
  safety: string;
};

export type FixtureComponent = {
  id: string;
  name: string;
  bounds: Bounds;
  setup_id?: string | null;
  work_axis?: Vec3 | null;
  kind?: "fixture" | "sacrificial" | "machine";
};

export type SafetyConfiguration = {
  clearance_mm: number;
  vise_grip_height_mm: number;
  fixture_strategy: "vise" | "sacrificial_plate";
  support_thickness_mm: number;
  fixture_components: FixtureComponent[];
};

export type CollisionResult = {
  schema_version: string;
  engine: string;
  status: "passed" | "failed";
  configuration: SafetyConfiguration;
  checks: { id: string; status: "passed" | "failed"; message: string }[];
  collisions: { kind: string; operation_id: string; target_id: string; position: Vec3 }[];
  low_rapids: { operation_id: string; minimum_z: number; required_z: number }[];
  metrics: { fixture_component_count: number; collision_count: number; low_rapid_count: number; required_rapid_z: number; support_penetration_mm?: number };
  limitations: string[];
};

export type SimulationResult = {
  schema_version: string;
  engine: string;
  status: "completed" | "warning";
  method: string;
  metrics: {
    initial_stock_volume_mm3: number;
    removed_volume_mm3: number;
    remaining_volume_mm3: number;
    removed_percent: number;
    cut_segment_count: number;
    resolution_mm: number;
  };
  surface: SimulationSurface;
  setup_surfaces?: SimulationSurface[];
  warnings: string[];
};

export type SimulationSurface = {
    setup_id?: string;
    work_axis?: Vec3;
    frame?: { x: Vec3; y: Vec3; z: Vec3 };
    origin: { x: number; y: number };
    bottom_z: number;
    top_z: number;
    resolution_mm: number;
    columns: number;
    rows: number;
    heights: number[];
    cut_segment_count?: number;
    removed_volume_mm3?: number;
    stock_volume_mm3?: number;
};

export type VerificationResult = {
  schema_version: string;
  engine: string;
  status: "passed" | "warning" | "failed";
  checks: { id: string; status: "passed" | "warning" | "failed"; message: string }[];
  errors: string[];
  warnings: string[];
  metrics: {
    extent_mm: Vec3;
    estimated_cycle_minutes: number;
    generated_operation_count: number;
  };
  limitations: string[];
};

export type MaterialProfile = {
  id: string; name: string; milling_speed_m_min: number; drilling_speed_m_min: number;
  mill_feed_per_tooth_mm: number; drill_feed_per_rev_mm: number;
};

export type MachineProfile = {
  id: string; name: string; axes: number; travel_mm: number[];
  max_spindle_rpm: number; max_feed_mm_min: number; max_tool_diameter_mm: number;
  postprocessor?: string | null;
};

export type Catalogs = {
  schema_version: string;
  materials: MaterialProfile[];
  machines: MachineProfile[];
  tools: Tool[];
  operations: OperationDefinition[];
};

export type OperationParameterDefinition = {
  key: string;
  label: string;
  type: "number" | "integer" | "boolean" | "enum";
  group: "geometry" | "cutting" | "non_cutting" | "strategy";
  unit: string | null;
  required: boolean;
  default: string | number | boolean | null;
  minimum: number | null;
  maximum: number | null;
  choices: string[];
};

export type OperationDefinition = {
  id: string;
  version: number;
  name: string;
  category: string;
  description: string;
  maturity: "planned" | "experimental" | "generated" | "validated" | "production";
  manual_enabled: boolean;
  geometry: { accepts: string[]; minimum_selection: number; maximum_selection: number | null };
  tool: { accepts: string[]; default_tool_id: string };
  parameters: OperationParameterDefinition[];
  engine: { provider: string; operation: string; modifiers: string[] };
};

export type Tool = {
  id: string; name: string; kind: string; diameter_mm: number; flute_count: number; max_rpm: number; catalog_match: boolean;
  flute_length_mm: number; stickout_mm: number; holder_diameter_mm: number;
};

export type Operation = {
  id: string;
  sequence: number;
  type: string;
  name: string;
  feature_ids: string[];
  tool: Tool;
  parameters: Record<string, string | number | boolean>;
  rationale: string[];
  confidence: number;
  status: "proposed" | "approved" | "warning";
  definition_id: string | null;
  definition_version: number;
  source: "manual" | "automatic" | "template" | "recommendation";
  enabled: boolean;
  generation_state: "dirty" | "generating" | "generated" | "failed";
};

export type Setup = {
  id: string;
  name: string;
  work_axis: Vec3;
  datum_feature_id: string | null;
  fixture: string;
  operations: Operation[];
};

export type Job = {
  id: string;
  status: "processing" | "completed" | "failed";
  filename: string;
  created_at: string;
  material: string;
  machine: string;
  model_url: string | null;
  error: string | null;
  analysis: {
    topology: Record<string, number>;
    measurements: Record<string, unknown>;
    planar_features: PlanarFeature[];
    cylindrical_features: CylindricalFeature[];
    prismatic_features: PrismaticFeature[];
    visual_edges: Vec3[][];
  } | null;
  plan: {
    title: string;
    material: string;
    machine: string;
    material_profile: MaterialProfile | null;
    machine_profile: MachineProfile | null;
    safety: SafetyConfiguration | null;
    stock: Record<string, unknown>;
    setups: Setup[];
    warnings: string[];
    assumptions: string[];
    estimated_minutes: number;
    automation_status: "ready" | "review" | "unsupported";
    blocking_reasons: string[];
  } | null;
};
