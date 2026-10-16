// The Demo page's consultation engine (mock/mockConsult.ts) is a copy of the server's. These cases, and what the
// server's engine makes of them, are written by the backend's tests (tests/test_consult.py), so the two cannot drift.
import { describe, expect, it } from "vitest";
import { evaluateAll, settingsOf, type AnswerRecord } from "../mock/mockConsult";
import cases from "./consultParity.json";

interface Case {
  name: string;
  facts: Record<string, unknown>;
  records: Partial<AnswerRecord>[];
  expected: {
    questions: Record<string, { applies: boolean; status: string; value: unknown; recommendation: unknown }>;
    next: string | null;
    blockers: string[];
    conflicts: { id: string; status: string; message: string }[];
    settings: Record<string, unknown>;
  };
}

describe("the Demo consultation engine", () => {
  for (const c of cases as unknown as Case[]) {
    it(`agrees with the server's on: ${c.name}`, () => {
      const records = c.records.map((r) => ({ by: { kind: "person", id: "p" }, at: "2026-01-01T00:00:00+00:00", catalogVersion: "1.0.0", ...r }) as AnswerRecord);
      const st = evaluateAll(c.facts, records);
      const questions = Object.fromEntries([...st.questions.entries()].map(([id, q]) =>
        [id, { applies: q.applies, status: q.status, value: q.value ?? null, recommendation: q.recommendation ?? null }]));
      expect(questions).toEqual(c.expected.questions);
      expect(st.next).toBe(c.expected.next);
      expect(st.blockers).toEqual(c.expected.blockers);
      expect(st.conflicts.map((x) => ({ id: x.id, status: x.status, message: x.message }))).toEqual(c.expected.conflicts);
      expect(settingsOf(st)).toEqual(c.expected.settings);
    });
  }
});
