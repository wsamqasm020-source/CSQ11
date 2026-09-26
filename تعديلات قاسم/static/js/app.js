/**
 * QR Attendance System - Main Application JavaScript
 * Handles offline functionality and data synchronization
 */

(function() {
    'use strict';

    // App State
    const AppState = {
        isOnline: navigator.onLine,
        isSyncing: false,
        pendingSync: [],
        lastSync: null
    };

    // Database wrapper for offline storage
    const OfflineDB = {
        dbName: 'QRAttendanceDB',
        dbVersion: 2,
        db: null,

        async init() {
            return new Promise((resolve, reject) => {
                const request = indexedDB.open(this.dbName, this.dbVersion);
                
                request.onerror = () => reject(request.error);
                request.onsuccess = () => {
                    this.db = request.result;
                    resolve(this.db);
                };
                
                request.onupgradeneeded = (event) => {
                    const db = event.target.result;
                    
                    if (!db.objectStoreNames.contains('pendingAttendance')) {
                        const store = db.createObjectStore('pendingAttendance', { keyPath: 'id', autoIncrement: true });
                        store.createIndex('timestamp', 'timestamp', { unique: false });
                        store.createIndex('synced', 'synced', { unique: false });
                        store.createIndex('student_id', 'student_id', { unique: false });
                    } else {
                        const store = event.target.transaction.objectStore('pendingAttendance');
                        if (!store.indexNames.contains('student_id')) {
                            store.createIndex('student_id', 'student_id', { unique: false });
                        }
                    }
                    
                    if (!db.objectStoreNames.contains('students')) {
                        const store = db.createObjectStore('students', { keyPath: 'id' });
                        store.createIndex('student_code', 'student_code', { unique: true });
                    }
                    
                    if (!db.objectStoreNames.contains('subjects')) {
                        db.createObjectStore('subjects', { keyPath: 'id' });
                    }
                    
                    if (!db.objectStoreNames.contains('offlineAttendance')) {
                        const store = db.createObjectStore('offlineAttendance', { keyPath: 'id', autoIncrement: true });
                        store.createIndex('timestamp', 'timestamp', { unique: false });
                        store.createIndex('synced', 'synced', { unique: false });
                    }
                };
            });
        },

        async addPendingAttendance(record) {
            if (!this.db) await this.init();
            return new Promise((resolve, reject) => {
                const transaction = this.db.transaction(['pendingAttendance'], 'readwrite');
                const store = transaction.objectStore('pendingAttendance');
                const request = store.add({
                    ...record,
                    timestamp: new Date().toISOString(),
                    synced: false
                });
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(request.error);
            });
        },

        async getPendingAttendance() {
            if (!this.db) await this.init();
            return new Promise((resolve, reject) => {
                const transaction = this.db.transaction(['pendingAttendance'], 'readonly');
                const store = transaction.objectStore('pendingAttendance');
                const request = store.getAll();
                request.onsuccess = () => {
                    const allRecords = request.result;
                    const pendingRecords = allRecords.filter(record => record.synced === false);
                    resolve(pendingRecords);
                };
                request.onerror = () => reject(request.error);
            });
        },

        async markAsSynced(id) {
            if (!this.db) await this.init();
            return new Promise((resolve, reject) => {
                const transaction = this.db.transaction(['pendingAttendance'], 'readwrite');
                const store = transaction.objectStore('pendingAttendance');
                const request = store.delete(id);
                request.onsuccess = () => resolve();
                request.onerror = () => reject(request.error);
            });
        },

        async saveOfflineAttendance(record) {
            if (!this.db) await this.init();
            return new Promise((resolve, reject) => {
                const transaction = this.db.transaction(['offlineAttendance'], 'readwrite');
                const store = transaction.objectStore('offlineAttendance');
                const request = store.add({
                    ...record,
                    timestamp: new Date().toISOString(),
                    synced: false
                });
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(request.error);
            });
        },

        async getOfflineAttendance() {
            if (!this.db) await this.init();
            return new Promise((resolve, reject) => {
                const transaction = this.db.transaction(['offlineAttendance'], 'readonly');
                const store = transaction.objectStore('offlineAttendance');
                const request = store.getAll();
                request.onsuccess = () => {
                    const allRecords = request.result;
                    const pendingRecords = allRecords.filter(record => record.synced === false);
                    resolve(pendingRecords);
                };
                request.onerror = () => reject(request.error);
            });
        },

        async cacheStudents(students) {
            if (!this.db) await this.init();
            const transaction = this.db.transaction(['students'], 'readwrite');
            const store = transaction.objectStore('students');
            for (const student of students) {
                store.put(student);
            }
        },

        async getCachedStudents() {
            if (!this.db) await this.init();
            return new Promise((resolve, reject) => {
                const transaction = this.db.transaction(['students'], 'readonly');
                const store = transaction.objectStore('students');
                const request = store.getAll();
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(request.error);
            });
        },

        async getCachedStudentByCode(studentCode) {
            if (!this.db) await this.init();
            return new Promise((resolve, reject) => {
                const transaction = this.db.transaction(['students'], 'readonly');
                const store = transaction.objectStore('students');
                const index = store.index('student_code');
                const request = index.get(studentCode);
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(request.error);
            });
        }
    };

    // Network status handler
    function handleNetworkChange() {
        AppState.isOnline = navigator.onLine;
        const event = new CustomEvent('networkChange', { detail: { isOnline: AppState.isOnline } });
        window.dispatchEvent(event);
        
        if (AppState.isOnline) {
            console.log('[App] Connection restored');
            syncPendingData();
        } else {
            console.log('[App] Connection lost');
        }
    }

    // Sync pending data when back online - SILENT MODE
    async function syncPendingData() {
        if (AppState.isSyncing || !AppState.isOnline) return;
        
        AppState.isSyncing = true;
        try {
            const pending = await OfflineDB.getPendingAttendance();
            
            if (pending.length === 0) {
                AppState.isSyncing = false;
                return;
            }

            // مزامنة صامتة بدون إشعارات
            for (const record of pending) {
                try {
                    const response = await fetch('/api/scan-qr', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            qr_data: record.qr_data,
                            subject_id: record.subject_id
                        })
                    });
                    
                    if (response.ok) {
                        await OfflineDB.markAsSynced(record.id);
                        console.log('[Sync] Record synced silently:', record.id);
                    } else {
                        console.error('[Sync] Failed to sync record:', await response.text());
                    }
                } catch (error) {
                    console.error('[Sync] Network error during sync:', error);
                    break;
                }
            }
            
            AppState.lastSync = new Date();
            console.log('[Sync] Silent sync completed at', AppState.lastSync);
        } finally {
            AppState.isSyncing = false;
        }
    }

    // Initialize
    document.addEventListener('DOMContentLoaded', async function() {
        try {
            await OfflineDB.init();
            console.log('[App] IndexedDB initialized');
        } catch (error) {
            console.error('[App] Failed to initialize IndexedDB:', error);
        }

        window.addEventListener('online', handleNetworkChange);
        window.addEventListener('offline', handleNetworkChange);

        if (navigator.onLine) {
            syncPendingData();
        }
    });

    // Expose to global scope
    window.QRAttendance = {
        AppState,
        OfflineDB,
        syncPendingData
    };

})();