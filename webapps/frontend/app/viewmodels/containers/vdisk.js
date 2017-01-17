// Copyright (C) 2016 iNuron NV
//
// This file is part of Open vStorage Open Source Edition (OSE),
// as available from
//
//      http://www.openvstorage.org and
//      http://www.openvstorage.com.
//
// This file is free software; you can redistribute it and/or modify it
// under the terms of the GNU Affero General Public License v3 (GNU AGPLv3)
// as published by the Free Software Foundation, in version 3 as it comes
// in the LICENSE.txt file of the Open vStorage OSE distribution.
//
// Open vStorage is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY of any kind.
/*global define */
define([
    'jquery', 'knockout',
    'ovs/generic', 'ovs/api', 'ovs/shared', '../containers/edgeclient'
], function($, ko, generic, api, shared, EdgeClient) {
    "use strict";
    return function(guid) {
        var self = this;

        // Variables
        self.shared = shared;

        // Handles
        self.loadConfigHandle        = undefined;
        self.loadHandle              = undefined;
        self.loadStorageRouterHandle = undefined;

        // External dependencies
        self.domainsPresent     = ko.observable(false);
        self.dtlTargets         = ko.observableArray([]);
        self.storageRouter      = ko.observable();
        self.storageRouterGuids = ko.observableArray([]);
        self.vpool              = ko.observable();

        // Observables
        self.backendRead       = ko.observable().extend({ smooth: {} }).extend({ format: generic.formatBytes });
        self.backendWritten    = ko.observable().extend({ smooth: {} }).extend({ format: generic.formatBytes });
        self.bandwidthSaved    = ko.observable().extend({ smooth: {} }).extend({ format: generic.formatBytes });
        self.cacheHits         = ko.observable().extend({ smooth: {} }).extend({ format: generic.formatNumber });
        self.cacheMisses       = ko.observable().extend({ smooth: {} }).extend({ format: generic.formatNumber });
        self.childrenGuids     = ko.observableArray([]);
        self.deviceName        = ko.observable();
        self.dtlEnabled        = ko.observable(true);
        self.dtlManual         = ko.observable();
        self.dtlMode           = ko.observable();
        self.dtlStatus         = ko.observable();
        self.dtlTarget         = ko.observableArray([]);
        self.edgeClients       = ko.observableArray([]);
        self.guid              = ko.observable(guid);
        self.iops              = ko.observable().extend({ smooth: {} }).extend({ format: generic.formatNumber });
        self.isVTemplate       = ko.observable();
        self.loaded            = ko.observable(false);
        self.loading           = ko.observable(false);
        self.loadingConfig     = ko.observable(false);
        self.name              = ko.observable();
        self.namespace         = ko.observable();
        self.oldConfiguration  = ko.observable();
        self.parentVDiskGuid   = ko.observable();
        self.readSpeed         = ko.observable().extend({ smooth: {} }).extend({ format: generic.formatSpeed });
        self.scoSize           = ko.observable(4);
        self.scoSizes          = ko.observableArray([4, 8, 16, 32, 64, 128]);
        self.size              = ko.observable().extend({ smooth: {} }).extend({ format: generic.formatBytes });
        self.snapshots         = ko.observableArray([]);
        self.storageRouterGuid = ko.observable();
        self.storedData        = ko.observable().extend({ smooth: {} }).extend({ format: generic.formatBytes });
        self.totalCacheHits    = ko.observable().extend({ smooth: {} }).extend({ format: generic.formatNumber });
        self.volumeId          = ko.observable();
        self.vpoolGuid         = ko.observable();
        self.writeSpeed        = ko.observable().extend({ smooth: {} }).extend({ format: generic.formatSpeed });
        self.writeBuffer       = ko.observable(128).extend({numeric: {min: 128, max: 10240}});

        // Computed
        self.dtlModes = ko.computed(function() {
            return [
                {name: 'no_sync', disabled: false},
                {name: 'a_sync', disabled: self.storageRouterGuids().length <= 1 || (self.dtlTargets().length === 0 && self.domainsPresent())},
                {name: 'sync', disabled: self.storageRouterGuids().length <= 1 || (self.dtlTargets().length === 0 && self.domainsPresent())}
            ];
        });
        self.dtlModeChange = ko.computed({
            read: function() {
                if (self.storageRouterGuids().length <= 1 || (self.dtlTargets().length === 0 && self.domainsPresent())) {
                    self.dtlMode('no_sync');
                    return {name: self.dtlMode(), disabled: true};
                }
                return {name: self.dtlMode(), disabled: false};
            },
            write: function(mode) {
                if (mode.name === 'no_sync') {
                    self.dtlEnabled(false);
                    self.dtlTarget([]);
                } else {
                    self.dtlEnabled(true);
                    if (self.storageRouterGuids().length <= 1 || (self.dtlTargets().length === 0 && self.domainsPresent())) {
                        self.dtlTarget([]);
                        $.each(self.dtlModes(), function (index, item) {
                            item.disabled = true;
                        })
                    } else if (self.dtlManual() === true) {
                        self.dtlTarget(self.dtlTargets());
                    }
                }
                self.dtlMode(mode.name);
            }
        });
        self.configuration = ko.computed({
            read: function() {
                return {
                    sco_size: self.scoSize(),
                    dtl_mode: self.dtlEnabled() === true ? self.dtlMode() : 'no_sync',
                    write_buffer: self.writeBuffer(),
                    dtl_target: self.dtlTarget().slice()
                }
            },
            write: function(configData) {
                self.writeBuffer(Math.round(configData.write_buffer));
                self.scoSize(configData.sco_size);
                self.dtlMode(configData.dtl_mode);
                self.dtlTarget(self.dtlManual() ? configData.dtl_target.slice() : []);
            }
        });
        self.scoSize.subscribe(function(size) {
            if (size < 128) {
                self.writeBuffer.min = 128;
            } else {
                self.writeBuffer.min = 256;
            }
            self.writeBuffer(self.writeBuffer());
        });
        self.dtlTarget.subscribe(function(targets) {
            if (self.dtlTarget().length !== targets.length) {
                if (targets.length > 0) {
                    self.dtlManual(true);
                } else {
                    self.dtlManual(false);
                }
            }
        });
        self.configChanged = ko.computed(function() {
            var changed = false;
            if (self.oldConfiguration() !== undefined) {
                $.each(self.oldConfiguration(), function (key, _) {
                    if (!self.configuration().hasOwnProperty(key)) {
                        changed = true;
                        return false;
                    }
                    if ((self.configuration()[key] instanceof Array && !self.configuration()[key].equals(self.oldConfiguration()[key])) ||
                        (!(self.configuration()[key] instanceof Array) && self.configuration()[key] !== self.oldConfiguration()[key])) {
                        changed = true;
                        return false;
                    }
                });
            }
            return changed;
        });
        self.connectedECText = ko.computed(function() {
            if (self.edgeClients().length === 0) {
                return $.t('ovs:generic.noclients');
            }
            var clients = [];
            $.each(self.edgeClients(), function(index, client) {
                if (clients.length >= 5) {
                    clients.push($.t('ovs:generic.xmore', { amount: self.edgeClients().length - 5 }));
                    return false;
                }
                clients.push(client.ip() + ':' + client.port());
            });
            return clients.join(', ')
        });

        // Functions
        self.fillData = function(data) {
            generic.trySet(self.name, data, 'name');
            generic.trySet(self.size, data, 'size');
            generic.trySet(self.volumeId, data, 'volume_id');
            generic.trySet(self.dtlStatus, data, 'dtl_status');
            generic.trySet(self.vpoolGuid, data, 'vpool_guid');
            generic.trySet(self.dtlManual, data, 'has_manual_dtl');
            generic.trySet(self.isVTemplate, data, 'is_vtemplate');
            generic.trySet(self.childrenGuids, data, 'child_vdisks_guids');
            generic.trySet(self.parentVDiskGuid, data, 'parent_vdisk_guid');
            generic.trySet(self.storageRouterGuid, data, 'storagerouter_guid');
            if (data.hasOwnProperty('devicename')) {
                self.deviceName(data.devicename.replace(/^\//, ''));
            }
            if (data.hasOwnProperty('edge_clients')) {
                var keys = [], cdata = {};
                $.each(data.edge_clients, function (index, item) {
                    keys.push(item.key);
                    cdata[item.key] = item;
                });
                generic.crossFiller(
                    keys, self.edgeClients,
                    function (key) {
                        return new EdgeClient(key);
                    }, 'key'
                );
                $.each(self.edgeClients(), function (index, client) {
                    if (cdata.hasOwnProperty(client.key())) {
                        client.fillData(cdata[client.key()]);
                    }
                });
                self.edgeClients.sort(function (a, b) {
                    return a.key() < b.key() ? -1 : (a.key() > b.key() ? 1 : 0);
                });
            }
            if (data.hasOwnProperty('snapshots')) {
                var snapshots = [];
                $.each(data.snapshots, function(index, snapshot) {
                    if (snapshot.in_backend) {
                        snapshots.push(snapshot);
                    }
                });
                self.snapshots(snapshots);
            }
            if (data.hasOwnProperty('info')) {
                self.storedData(data.info.stored);
                self.namespace(data.info.namespace);
            }
            if (data.hasOwnProperty('statistics')) {
                var stats = data.statistics;
                self.iops(stats['4k_operations_ps']);
                self.cacheHits(stats.cache_hits_ps);
                self.cacheMisses(stats.cache_misses_ps);
                self.totalCacheHits(stats.cache_hits);
                self.readSpeed(stats.data_read_ps);
                self.writeSpeed(stats.data_written_ps);
                self.backendWritten(stats.backend_data_written);
                self.backendRead(stats.backend_data_read);
                self.bandwidthSaved(Math.max(0, stats.data_read - stats.backend_data_read));
            }
            self.snapshots.sort(function(a, b) {
                // Sorting based on newest first
                return b.timestamp - a.timestamp;
            });
            self.loaded(true);
            self.loading(false);
        };
        self.load = function() {
            return $.Deferred(function(deferred) {
                self.loading(true);
                if (generic.xhrCompleted(self.loadHandle)) {
                    self.loadHandle = api.get('vdisks/' + self.guid())
                        .done(function(data) {
                            self.fillData(data);
                            deferred.resolve();
                        })
                        .fail(deferred.reject)
                        .always(function() {
                            self.loading(false);
                        });
                } else {
                    deferred.reject();
                }
            }).promise();
        };
        self.loadConfiguration = function(reload) {
            if (reload === true) {
                self.loadingConfig(true);
            }
            return $.Deferred(function(deferred) {
                if (generic.xhrCompleted(self.loadConfigHandle)) {
                    self.loadConfigHandle = api.get('vdisks/' + self.guid() + '/get_config_params')
                        .then(self.shared.tasks.wait)
                        .done(function (data) {
                            if (data.hasOwnProperty('pagecache_ratio')) {
                                delete data['pagecache_ratio'];
                            }
                            self.dtlEnabled(data.dtl_mode !== 'no_sync');
                            self.configuration(data);
                            data = self.configuration(); // Pass through the getter/setter for possible cleanups
                            if (self.oldConfiguration() === undefined) {
                                self.oldConfiguration($.extend({}, data)); // Used to make comparison to check for changes
                                $.each(self.oldConfiguration(), function (key, _) {
                                    if (key === 'write_buffer') {
                                        var oldConfig = self.oldConfiguration();
                                        oldConfig.write_buffer = Math.round(self.oldConfiguration().write_buffer);
                                        self.oldConfiguration(oldConfig);
                                    }
                                });
                            }
                            deferred.resolve();
                        })
                        .fail(deferred.reject)
                        .always(function () {
                            self.loadingConfig(false);
                        })
                }
            }).promise();
        };
    };
});
