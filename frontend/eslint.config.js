import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      // eslint 10 needs eslint-plugin-react-hooks 7, whose recommended
      // preset adds the React Compiler rules (set-state-in-effect, refs,
      // purity and others) to these two. They flag existing components and
      // are a change of their own, so this keeps the two rules, at the same
      // levels, that the preset held before.
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "react-refresh/only-export-components": [
        "warn",
        { allowConstantExport: true },
      ],
      "@typescript-eslint/no-unused-vars": [
        "error",
        { varsIgnorePattern: "^_", argsIgnorePattern: "^_", caughtErrorsIgnorePattern: "^_" },
      ],
    },
  },
  {
    // The router module declares its lazy() page components next to the
    // router it exports, so Fast Refresh reloads the page when it changes.
    // That is all it needs: each page lives in its own file, which Fast
    // Refresh does swap. eslint-plugin-react-refresh 0.5 counts lazy()
    // results as components and would flag every route here.
    files: ["src/router/index.tsx"],
    rules: {
      "react-refresh/only-export-components": "off",
    },
  }
);
