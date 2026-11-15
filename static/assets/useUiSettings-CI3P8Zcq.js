<<<<<<<< HEAD:static/assets/useUiSettings-QPurze9x.js
import{a as e}from"./client-MyvT0tak.js";import{t}from"./useQuery-CHOG_Fbw.js";import{n}from"./AuthContext-Db9wjW6t.js";function r(r={}){let{hasPermission:i}=n(),a=i(`settings:read`);return t({queryKey:a?[`settings`]:[`ui-settings`],queryFn:a?()=>e.getSettings():()=>e.getUiSettings(),...r})}function i(e){return!!(e?.gcode_snippets||e?.has_gcode_snippets)}export{r as n,i as t};
========
import{a as e}from"./client-BcKCPdvL.js";import{t}from"./useQuery-CbpGE3Y4.js";import{n}from"./AuthContext-HPSJiZsj.js";function r(r={}){let{hasPermission:i}=n(),a=i(`settings:read`);return t({queryKey:a?[`settings`]:[`ui-settings`],queryFn:a?()=>e.getSettings():()=>e.getUiSettings(),...r})}function i(e){return!!(e?.gcode_snippets||e?.has_gcode_snippets)}export{r as n,i as t};
>>>>>>>> 8e5a34a9e (feat(camera): add dual-camera support with draggable floating overlays and source toggle):static/assets/useUiSettings-CI3P8Zcq.js
