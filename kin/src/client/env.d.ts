declare module '*.css' {
  const text: string
  export default text
}

// DSH's page hands every client plugin its react-dom (build.mjs, SHARED); the page uses this one function.
declare module 'react-dom' {
  export function flushSync<R>(fn: () => R): R
}
