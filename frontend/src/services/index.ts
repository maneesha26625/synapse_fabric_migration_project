import { mockApi } from "../mock/mockApi";
import type { ApiMode, MigrationApi } from "../types";
import { realApi } from "./realApi";

const STORAGE_KEY = "ma.apiMode"; // a non-sensitive UI preference

export function initialMode(): ApiMode {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved === "real" || saved === "mock") return saved;
  } catch {
    // storage unavailable: fall through to the build default
  }
  return import.meta.env.VITE_API_MODE === "mock" ? "mock" : "real";
}

export function rememberMode(mode: ApiMode): void {
  try {
    localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // ignore
  }
}

export function apiFor(mode: ApiMode): MigrationApi {
  return mode === "mock" ? mockApi : realApi;
}
