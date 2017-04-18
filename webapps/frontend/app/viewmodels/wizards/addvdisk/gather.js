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
    'ovs/api', 'ovs/generic', 'ovs/refresher', 'ovs/shared',
    './data', '../../containers/vpool', '../../containers/storagerouter'
], function($, ko,
            api, generic, Refresher, shared,
            data, VPool, StorageRouter) {
    "use strict";
    return function() {
        var self = this;

        // Variables
        self.data      = data;
        self.refresher = new Refresher();
        self.shared    = shared;

        // Handles
        self.loadAlbaBackendHandle    = undefined;
        self.loadStorageRoutersHandle = undefined;
        self.loadVPoolsHandle         = undefined;

        // Observables
        self.loading                  = ko.observable(false);
        self.loadingBackend           = ko.observable(false);
        self.preValidateBackendPreset = ko.observable();
        self.preValidateResult        = ko.observable({reasons: [], fields: []});

        // Computed
        self.canContinue = ko.computed(function() {
            var showErrors = true, reasons = [], fields = [], maxSize = self.data.sizeEntry.max * Math.pow(1024, 3),
                preValidation = self.preValidateResult(),
                backendPresetValidation = self.preValidateBackendPreset();
            if (preValidation.reasons.length > 0) {
                reasons = reasons.concat(preValidation.reasons);
                fields = fields.concat(preValidation.fields);
            }
            if (backendPresetValidation !== undefined) {
                reasons.push(backendPresetValidation);
            }
            if (self.data.name() === '') {
                fields.push('name');
                reasons.push($.t('ovs:wizards.add_vdisk.gather.invalid_name'));
            }
            if (self.data.vPool() === undefined) {
                fields.push('vpool');
                reasons.push($.t('ovs:wizards.add_vdisk.gather.invalid_vpool'));
            }
            if (self.data.storageRouter() === undefined) {
                fields.push('storageouter');
                reasons.push($.t('ovs:wizards.add_vdisk.gather.invalid_storagerouter'));
            }
            if (self.data.size() > maxSize) {
                fields.push('size');
                reasons.push($.t('ovs:wizards.add_vdisk.gather.invalid_size', {amount: parseInt(self.data.sizeEntry.max / 1024), unit: $.t('ovs:generic.units.tib')}));
            }
            if (self.loadingBackend()) {
                showErrors = false;  // Not able to continue because Backend preset information being loaded
                reasons.push('Loading Backend');
            }
            return { value: reasons.length === 0, showErrors: showErrors, reasons: reasons, fields: fields };
        });
        self.cleanedName = ko.computed(function() {
            return generic.cleanDeviceName(self.data.name());
        });
        self.storageRoutersByVpool = ko.computed(function() {
            if (self.data.vPool() === undefined) {
                self.data.storageRouter(undefined);
                return [];
            }
            var guids = [], result = [];
            $.each(self.data.storageRouters(), function(index, storageRouter) {
                if (storageRouter.vPoolGuids().contains(self.data.vPool().guid())) {
                    result.push(storageRouter);
                    guids.push(storageRouter.guid());
                }
            });
            if (self.data.storageRouter() !== undefined) {
                if (result.length > 0 && !guids.contains(self.data.storageRouter().guid())) {
                    self.data.storageRouter(result[0]);
                } else if (result.length === 0) {
                    self.data.storageRouter(undefined);
                }
            }
            return result;
        });

        // Functions
        self.preValidate = function() {
            var validationResult = {reasons: [], fields: []};
            return $.Deferred(function(deferred) {
                if (self.data.vPool() === undefined || self.data.name() === undefined) {
                    deferred.reject();
                    return;
                }
                api.get('vpools/' + self.data.vPool().guid() + '/devicename_exists', { queryparams: { name: self.data.name() }})
                    .done(function(exists) {
                        if (exists) {
                            validationResult.reasons.push($.t('ovs:wizards.add_vdisk.gather.name_exists'));
                            validationResult.fields.push('name');
                            self.preValidateResult(validationResult);
                            deferred.reject();
                        } else {
                            self.preValidateResult(validationResult);
                            deferred.resolve();
                        }
                    })
            }).promise();
        };
        self.loadVPools = function() {
            return $.Deferred(function(deferred) {
                generic.xhrAbort(self.loadVPoolsHandle);
                self.loadVPoolsHandle = api.get('vpools', {queryparams: {contents: ''}})
                    .done(function (data) {
                        var guids = [], vpData = {};
                        $.each(data.data, function (index, item) {
                            if (item.status === 'RUNNING') {
                                guids.push(item.guid);
                                vpData[item.guid] = item;
                            }
                        });
                        generic.crossFiller(
                            guids, self.data.vPools,
                            function (guid) {
                                return new VPool(guid);
                            }, 'guid'
                        );
                        $.each(self.data.vPools(), function (index, vpool) {
                            if (guids.contains(vpool.guid())) {
                                vpool.fillData(vpData[vpool.guid()]);
                                if (self.data.vPool() === undefined) {
                                    self.data.vPool(self.data.vPools()[0]);
                                } else if (!guids.contains(self.data.vPool().guid())) {
                                    self.data.vPool(self.data.vPools[0]);
                                }
                            }
                        });
                        if (self.data.vPools().length === 0) {
                            self.data.vPool(undefined);
                        }
                        deferred.resolve();
                    })
                    .fail(deferred.reject);
            });
        };
        self.loadStorageRouters = function() {
            return $.Deferred(function(deferred) {
                generic.xhrAbort(self.loadStorageRoutersHandle);
                self.loadStorageRoutersHandle = api.get('storagerouters', {queryparams: {contents: 'vpools_guids', sort: 'name'}})
                    .done(function(data) {
                        var guids = [], srdata = {};
                        $.each(data.data, function(index, item) {
                            guids.push(item.guid);
                            srdata[item.guid] = item;
                        });
                        generic.crossFiller(
                            guids, self.data.storageRouters,
                            function(guid) {
                                return new StorageRouter(guid);
                            }, 'guid'
                        );
                        $.each(self.data.storageRouters(), function(index, storageRouter) {
                            if (guids.contains(storageRouter.guid())) {
                                storageRouter.fillData(srdata[storageRouter.guid()]);
                                if (self.data.storageRouter() === undefined && self.data.vPool() !== undefined) {
                                    self.data.storageRouter(self.data.storageRouters()[0]);
                                }
                            }
                        });
                        deferred.resolve();
                    })
                    .fail(deferred.reject);
            });
        };
        self.finish = function() {
            return $.Deferred(function(deferred) {
                generic.alertInfo(
                    $.t('ovs:wizards.add_vdisk.gather.started'),
                    $.t('ovs:wizards.add_vdisk.gather.in_progress')
                );
                deferred.resolve();
                api.post('vdisks', {
                    data: {
                        name: self.data.name(),
                        size: self.data.size(),
                        vpool_guid: self.data.vPool().guid(),
                        storagerouter_guid: self.data.storageRouter().guid()
                    }
                })
                    .then(self.shared.tasks.wait)
                    .done(function() {
                        generic.alertSuccess(
                            $.t('ovs:wizards.add_vdisk.gather.complete'),
                            $.t('ovs:wizards.add_vdisk.gather.success')
                        );
                    })
                    .fail(function(error) {
                        error = generic.extractErrorMessage(error);
                        generic.alertError(
                            $.t('ovs:generic.error'),
                            $.t('ovs:wizards.add_vdisk.gather.failed', {why: error})
                        );
                    });
            }).promise();
        };

        // Subscriptions
        self.nameSubscription = self.data.name.subscribe(function() {
            if (!self.canContinue().value) {
                self.preValidateResult({reasons: [], fields: []});  // Reset the pre-validate result in case a vDisk name was specified which already existed
            }
        });
        self.vPoolSubscription = self.data.vPool.subscribe(function (vPool) {
            if (vPool === undefined || vPool.metadata() === undefined || !vPool.metadata().hasOwnProperty('backend')) {
                self.preValidateBackendPreset(undefined);
                return;
            }
            if (self.data.vPoolUsableBackendMap().hasOwnProperty(vPool.guid())) {
                if (self.data.vPoolUsableBackendMap()[vPool.guid()]) {
                    self.preValidateBackendPreset(undefined);
                } else {
                    self.preValidateBackendPreset($.t('ovs:wizards.add_vdisk.gather.invalid_preset'));
                }
                return;
            }

            self.loadingBackend(true);
            generic.xhrAbort(self.loadAlbaBackendHandle);
            var connectionInfo = vPool.metadata().backend.connection_info,
                getData = {ip: connectionInfo.host,
                           port: connectionInfo.port,
                           client_id: connectionInfo.client_id,
                           client_secret: connectionInfo.client_secret,
                           contents: 'presets'};
            self.loadAlbaBackendHandle = api.get('relay/alba/backends/' + vPool.metadata().backend.backend_info.alba_backend_guid, {queryparams: getData})
                .done(function (data) {
                    var usable_preset = false, map = self.data.vPoolUsableBackendMap();
                    $.each(data.presets, function(_, preset) {
                        if (preset.is_available === true) {
                            usable_preset = true;
                            return false;
                        }
                    });
                    map[vPool.guid()] = usable_preset;
                    self.data.vPoolUsableBackendMap(map);
                })
                .always(function() {
                    self.loadingBackend(false);
                    if (!self.data.vPoolUsableBackendMap().hasOwnProperty(vPool.guid()) || self.data.vPoolUsableBackendMap()[vPool.guid()] === true) {
                        self.preValidateBackendPreset(undefined);
                    } else {
                        self.preValidateBackendPreset($.t('ovs:wizards.add_vdisk.gather.invalid_preset'));
                    }
                });
        });

        // Durandal
        self.activate = function() {
            self.loading(true);
            self.refresher.init(function() {
                return self.loadVPools()
                    .then(self.loadStorageRouters)
                    .then(function() {
                        if (self.loading() === true) {
                            self.loading(false);
                        }
                    });
            }, 5000);
            self.refresher.run();
            self.refresher.start();
        };
        // self.deactivate = function() {
        //     self.vPoolSubscription.dispose();
        // }
    };
});
