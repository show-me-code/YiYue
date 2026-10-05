import { createRouter, createWebHashHistory } from 'vue-router'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', name: 'tasks', component: () => import('./views/TaskListView.vue') },
    { path: '/settings', name: 'settings', component: () => import('./views/SettingsView.vue') },
    { path: '/glossary', name: 'glossary', component: () => import('./views/GlossaryView.vue') },
    { path: '/reader/:id', name: 'reader', component: () => import('./views/ReaderView.vue') },
  ],
})

export default router
