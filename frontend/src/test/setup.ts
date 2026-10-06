import "@testing-library/jest-dom/vitest";

// jsdom has no layout, so page scrolling is a no-op here (its own stub logs "not implemented").
window.scrollTo = () => {};
