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
    'jquery', 'knockout', 'ovs/generic', './data'
], function ($, ko, generic, data) {
    "use strict";
    return function() {
        var self = this;

        // Variables
        self.data = data;

        // Observables
        self.clusterSizes      = ko.observableArray([4, 8, 16, 32, 64]);
        self.dtlModes          = ko.observableArray([{name: 'no_sync', disabled: false}, {name: 'a_sync', disabled: false}, {name: 'sync', disabled: false}]);
        self.dtlTransportModes = ko.observableArray([{name: 'tcp', disabled: false}, {name: 'rdma', disabled: true}]);
        self.scoSizes          = ko.observableArray([4, 8, 16, 32, 64, 128]);

        // Computed
        self.dtlMode = ko.computed({
            write: function(mode) {
                if (mode.name === 'no_sync') {
                    self.data.dtlEnabled(false);
                } else {
                    self.data.dtlEnabled(true);
                }
                self.data.dtlMode(mode);
            },
            read: function() {
                if (self.data.vPool() !== undefined && self.data.dtlEnabled() === false) {
                    return {name: 'no_sync', disabled: false};
                }
                return self.data.dtlMode();
            }
        });
        self.canContinue = ko.computed(function () {
            var reasons = [], fields = [];
            if (self.data.writeBufferGlobal() * 1024 * 1024 * 1024 > self.data.writeBufferGlobalMax()) {
                fields.push('writeBufferGlobal');
                reasons.push($.t('ovs:wizards.add_vpool.gather_config.over_allocation'));
            }

            // Verify amount of proxies to deploy is possible
            var total_available = 0, largest_ssd = 0, largest_sata = 0, amount_of_proxies = self.data.proxyAmount(), maximum = amount_of_proxies;
            if (self.data.partitions() !== undefined) {
                $.each(self.data.partitions()['WRITE'], function(_, value) {
                    total_available += value['available'];
                    if (value['ssd'] === true && value['available'] > largest_ssd) {
                        largest_ssd = value['available']
                    } else if (value['ssd'] === false && value['available'] > largest_sata) {
                        largest_sata = value['available']
                    }
                });
                if (self.data.useAA() === false && (self.data.fragmentCacheOnRead() === true || self.data.fragmentCacheOnWrite() === true)) {
                    var proportion = (largest_ssd || largest_sata) * 100.0 / total_available,
                        available = proportion * self.data.writeBufferGlobal() * Math.pow(1024, 3) / 100 * 0.10,  // Only 10% is used on the largest WRITE partition for fragment caching
                        fragment_size = available / amount_of_proxies;
                    if (fragment_size < Math.pow(1024, 3)) {
                        while (maximum > 0) {
                            if (available / maximum > Math.pow(1024, 3)) {
                                break;
                            }
                            maximum -= 1;
                        }
                        fields.push('writeBufferGlobal');
                        if (maximum == 0) {
                            reasons.push($.t('ovs:wizards.add_vpool.gather_config.fragment_cache_no_proxies'));
                        } else {
                            reasons.push($.t('ovs:wizards.add_vpool.gather_config.fragment_cache_too_small', {amount: maximum, multiple: maximum === 1 ? 'y' : 'ies'}));
                        }
                    }
                }
            }
            return { value: reasons.length === 0, reasons: reasons, fields: fields };
        });

        // Durandal
        self.activate = function() {
            $.each(self.data.storageRoutersAvailable(), function (index, storageRouter) {
                if (storageRouter === self.data.storageRouter()) {
                    $.each(self.dtlTransportModes(), function (i, key) {
                        if (key.name === 'rdma') {
                            self.dtlTransportModes()[i].disabled = storageRouter.rdmaCapable() === undefined ? true : !storageRouter.rdmaCapable();
                            return false;
                        }
                    });
                }
            });
        };
    };
});
