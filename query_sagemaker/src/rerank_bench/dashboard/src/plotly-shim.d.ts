// plotly.js-dist-min is the minified bundle; it has the same API as plotly.js
// so we re-export the types from @types/plotly.js.
declare module "plotly.js-dist-min" {
  export * from "plotly.js";
  import Plotly from "plotly.js";
  export default Plotly;
}
