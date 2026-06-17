// Screen Context Awareness v0.1 — a tiny mutable singleton any component can
// report into, read by ChatPanel at send time. No re-renders needed: the value
// only matters at the moment a chat message is sent.

export interface ScreenEntity {
  type:
    | "communication_draft"
    | "schedule_proposal"
    | "integration_intent"
    | "knowledge_source"
    | "job"
    | "execution_plan";
  id: string;
}

export interface ScreenContext {
  view: string;
  section?: string;
  label?: string;
  last_section?: string;
  entity?: ScreenEntity;
  last_entity?: ScreenEntity;
}

const state: ScreenContext = { view: "chat" };

// The chat panel is only visible on the chat view, so an entity the user was
// inspecting in the Admin Console must survive navigation back to chat —
// open entities demote to last_entity instead of being dropped.
function demoteEntity() {
  if (state.entity) {
    state.last_entity = state.entity;
    state.entity = undefined;
  }
}

export function setScreenView(view: string, section?: string, label?: string) {
  if (state.section && state.section !== section && state.view !== "chat") {
    state.last_section = state.section;
  }
  demoteEntity();
  state.view = view;
  state.section = section;
  state.label = label;
}

export function setScreenEntity(entity: ScreenEntity) {
  state.entity = entity;
}

export function clearScreenEntity() {
  demoteEntity();
}

export function getScreenContext(): ScreenContext {
  return {
    ...state,
    entity: state.entity ? { ...state.entity } : undefined,
    last_entity: state.last_entity ? { ...state.last_entity } : undefined,
  };
}
