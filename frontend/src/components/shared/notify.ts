/**
 * A short confirmation for the toast in the page frame ("Plan was reset").
 * Any component can call it; the layout shows it for a few seconds.
 */
export const TOAST_EVENT = "ma:toast";

export function notify(message: string): void {
  window.dispatchEvent(new CustomEvent<string>(TOAST_EVENT, { detail: message }));
}
