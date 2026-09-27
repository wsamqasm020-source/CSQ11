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
        isSyncingStudents: false,
        pendingSync: [],
        lastSync: null
    };

    // Database wrapper for offline storage
    const OfflineDB = {
        dbName: 'QRAttendanceDB',
        dbVersion: 3,
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

                    if (!db.objectStoreNames.contains('pendingStudents')) {
                        db.createObjectStore('pendingStudents', { keyPath: 'requestId' });
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
                    timestamp: record.timestamp || new Date().toISOString(),
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

        async updatePendingAttendance(id, changes) {
            if (!this.db) await this.init();
            return new Promise((resolve, reject) => {
                const transaction = this.db.transaction(['pendingAttendance'], 'readwrite');
                const store = transaction.objectStore('pendingAttendance');
                const request = store.get(id);
                request.onsuccess = () => {
                    if (request.result) store.put({ ...request.result, ...changes });
                };
                transaction.oncomplete = () => resolve();
                transaction.onerror = () => reject(transaction.error);
                transaction.onabort = () => reject(transaction.error);
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
            store.clear();
            for (const student of students) {
                store.put(student);
            }
        },

        async cacheSubjects(subjects) {
            if (!this.db) await this.init();
            return new Promise((resolve, reject) => {
                const transaction = this.db.transaction(['subjects'], 'readwrite');
                const store = transaction.objectStore('subjects');
                for (const subject of subjects) store.put(subject);
                transaction.oncomplete = () => resolve();
                transaction.onerror = () => reject(transaction.error);
                transaction.onabort = () => reject(transaction.error);
            });
        },

        async getCachedSubjects() {
            if (!this.db) await this.init();
            return new Promise((resolve, reject) => {
                const transaction = this.db.transaction(['subjects'], 'readonly');
                const request = transaction.objectStore('subjects').getAll();
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(request.error);
            });
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
        },

        async addPendingStudent(record) {
            if (!this.db) await this.init();
            return new Promise((resolve, reject) => {
                const transaction = this.db.transaction(['pendingStudents'], 'readwrite');
                transaction.objectStore('pendingStudents').put(record);
                transaction.oncomplete = () => resolve(record);
                transaction.onerror = () => reject(transaction.error);
                transaction.onabort = () => reject(transaction.error);
            });
        },

        async getPendingStudents() {
            if (!this.db) await this.init();
            return new Promise((resolve, reject) => {
                const request = this.db.transaction(['pendingStudents'], 'readonly')
                    .objectStore('pendingStudents').getAll();
                request.onsuccess = () => resolve(request.result);
                request.onerror = () => reject(request.error);
            });
        },

        async removePendingStudent(requestId) {
            if (!this.db) await this.init();
            return new Promise((resolve, reject) => {
                const transaction = this.db.transaction(['pendingStudents'], 'readwrite');
                transaction.objectStore('pendingStudents').delete(requestId);
                transaction.oncomplete = () => resolve();
                transaction.onerror = () => reject(transaction.error);
                transaction.onabort = () => reject(transaction.error);
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
            syncPendingStudents();
        } else {
            console.log('[App] Connection lost');
        }
    }

    function createEventId() {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return window.crypto.randomUUID();
        }
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, character => {
            const random = Math.random() * 16 | 0;
            return (character === 'x' ? random : (random & 0x3 | 0x8)).toString(16);
        });
    }

    function scheduleBackgroundSync() {
        if (!('serviceWorker' in navigator)) return;
        navigator.serviceWorker.ready.then(registration => {
            if (registration.sync) return registration.sync.register('sync-attendance');
        }).catch(error => console.warn('[Sync] Background sync unavailable:', error));
    }

    async function submitAttendance(endpoint, payload) {
        await OfflineDB.init();
        const eventId = payload.event_id || createEventId();
        const timestamp = new Date().toISOString();
        const queuedPayload = { ...payload, event_id: eventId };
        const queueId = await OfflineDB.addPendingAttendance({
            endpoint,
            payload: queuedPayload,
            timestamp,
            event_id: eventId
        });

        scheduleBackgroundSync();
        if (!navigator.onLine) return { queued: true, event_id: eventId };

        try {
            const response = await fetch(endpoint, {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(queuedPayload)
            });
            const result = await response.json().catch(() => ({}));
            if (result.success || result.already_registered || result.already_synced) {
                await OfflineDB.markAsSynced(queueId);
                return { queued: false, result };
            }

            if (response.status < 500 && !response.redirected &&
                response.headers.get('content-type')?.includes('application/json')) {
                await OfflineDB.markAsSynced(queueId);
                return { queued: false, result, error: result.message || 'تعذر تسجيل الحضور' };
            }
            return { queued: true, result, event_id: eventId };
        } catch (error) {
            return { queued: true, error, event_id: eventId };
        }
    }

    async function submitStudentRegistration(payload) {
        await OfflineDB.init();
        const requestId = payload.student_code;
        await OfflineDB.addPendingStudent({
            requestId,
            endpoint: '/api/generate-qr',
            payload,
            timestamp: new Date().toISOString()
        });
        scheduleBackgroundSync();

        if (!navigator.onLine) return { queued: true, requestId };

        try {
            const response = await fetch('/api/generate-qr', {
                method: 'POST',
                credentials: 'same-origin',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const result = await response.json().catch(() => ({}));
            if (response.ok && result.success) {
                await OfflineDB.removePendingStudent(requestId);
                return { queued: false, result };
            }
            if (response.status !== 401 && response.status < 500 && !response.redirected &&
                response.headers.get('content-type')?.includes('application/json')) {
                await OfflineDB.removePendingStudent(requestId);
                return { queued: false, error: result.message || 'تعذر حفظ الطالب' };
            }
            return { queued: true, requestId };
        } catch (error) {
            return { queued: true, requestId, error };
        }
    }

    async function syncPendingStudents() {
        if (AppState.isSyncingStudents || !navigator.onLine) return;
        AppState.isSyncingStudents = true;

        try {
            const pending = await OfflineDB.getPendingStudents();
            for (const record of pending) {
                try {
                    const response = await fetch(record.endpoint || '/api/generate-qr', {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(record.payload)
                    });
                    const result = await response.json().catch(() => ({}));

                    if (response.ok && result.success) {
                        await OfflineDB.removePendingStudent(record.requestId);
                        window.dispatchEvent(new CustomEvent('studentRegistrationSyncComplete', {
                            detail: { requestId: record.requestId, result }
                        }));
                        continue;
                    }

                    if (response.status !== 401 && response.status < 500 && !response.redirected &&
                        response.headers.get('content-type')?.includes('application/json')) {
                        await OfflineDB.removePendingStudent(record.requestId);
                        window.dispatchEvent(new CustomEvent('studentRegistrationSyncComplete', {
                            detail: { requestId: record.requestId, error: result.message || 'تعذر حفظ الطالب' }
                        }));
                    }
                    break;
                } catch (error) {
                    console.warn('[Sync] Student registration remains queued:', error);
                    break;
                }
            }
        } finally {
            AppState.isSyncingStudents = false;
        }
    }

    // Replay durable attendance records when connectivity returns.
    async function syncPendingData() {
        if (AppState.isSyncing || !AppState.isOnline) return;
        
        AppState.isSyncing = true;
        try {
            const pending = await OfflineDB.getPendingAttendance();
            
            if (pending.length === 0) {
                AppState.isSyncing = false;
                return;
            }

            let syncedCount = 0;
            for (const record of pending) {
                try {
                    const endpoint = record.endpoint || '/api/scan-qr';
                    const storedPayload = record.payload || {
                        qr_data: record.qr_data,
                        subject_id: record.subject_id
                    };
                    const eventId = storedPayload.event_id || record.event_id || createEventId();
                    const timestamp = record.timestamp || new Date().toISOString();
                    const payload = {
                        ...storedPayload,
                        event_id: eventId,
                        offline_sync: true,
                        scanned_at: timestamp
                    };
                    await OfflineDB.updatePendingAttendance(record.id, {
                        endpoint,
                        event_id: eventId,
                        payload: { ...storedPayload, event_id: eventId },
                        timestamp
                    });
                    const response = await fetch(endpoint, {
                        method: 'POST',
                        credentials: 'same-origin',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });

                    const result = await response.json().catch(() => ({}));
                    if (result.success || result.already_registered || result.already_synced) {
                        await OfflineDB.markAsSynced(record.id);
                        syncedCount += 1;
                        console.log('[Sync] Attendance synced:', record.id);
                    } else {
                        console.error('[Sync] Attendance remains queued:', result.message || response.status);
                    }
                } catch (error) {
                    console.error('[Sync] Network error during sync:', error);
                    break;
                }
            }
            
            AppState.lastSync = new Date();
            window.dispatchEvent(new CustomEvent('attendanceSyncComplete', {
                detail: { synced: syncedCount, remaining: (await OfflineDB.getPendingAttendance()).length }
            }));
            console.log('[Sync] Completed at', AppState.lastSync, 'records:', syncedCount);
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
        if ('serviceWorker' in navigator) {
            navigator.serviceWorker.addEventListener('message', event => {
                if (event.data && event.data.type === 'sync-attendance') {
                    syncPendingData();
                    syncPendingStudents();
                }
            });
        }

        document.addEventListener('visibilitychange', () => {
            if (!document.hidden && navigator.onLine) {
                syncPendingData();
                syncPendingStudents();
            }
        });
        window.setInterval(() => {
            if (navigator.onLine) {
                syncPendingData();
                syncPendingStudents();
            }
        }, 60000);

        if (navigator.onLine) {
            syncPendingData();
            syncPendingStudents();
        }
    });

    // Expose to global scope
    window.QRAttendance = {
        AppState,
        OfflineDB,
        syncPendingData,
        submitAttendance,
        submitStudentRegistration,
        syncPendingStudents
    };

})();