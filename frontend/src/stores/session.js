export const sessionStore={get token(){return localStorage.getItem('bm_token')},get user(){return JSON.parse(localStorage.getItem('bm_user')||'null')}};
