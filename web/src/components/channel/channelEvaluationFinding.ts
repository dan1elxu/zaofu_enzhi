/** Evaluation providers may use different names for a finding's prose. */
export function channelEvaluationFinding(value: Record<string, unknown>) {
  const text = (...fields: string[]) => fields.map((field) => value[field])
    .find((entry): entry is string => typeof entry === "string" && !!entry.trim())?.trim() ?? "";
  return {
    body: text("finding", "reason", "message", "description", "title") || "未提供发现正文，请查看原始数据。",
    severity: text("severity") || "未标注严重度",
    criterion: text("criterion_id") || (Array.isArray(value.criterion_ids)
      ? value.criterion_ids.filter((entry): entry is string => typeof entry === "string" && !!entry.trim())
        .map((entry) => entry.trim()).join(", ") : ""),
    source: text("source_location"),
    target: text("target_location"),
  };
}
