export type ArtifactType = 'chart' | 'pdf' | 'excel';

export interface Artifact {
  id: string;
  type: ArtifactType;
  filename: string | null;
  created_at: string | null;
  origin: 'task' | 'chat';
  task_id: string | null;
  session_id: string | null;
}

/** Helper compartilhado — porta `_artifactUrl()`/as URLs de `_addArtifactToChat()`. */
export function artifactUrl(type: ArtifactType, id: string): string {
  return `/${type}/${id}`;
}

export const ARTIFACT_LABELS: Record<ArtifactType, string> = { pdf: 'PDF', chart: 'Gráfico', excel: 'Excel' };
